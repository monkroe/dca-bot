#!/usr/bin/env python3
"""ROB-21: re-peg identity.

A re-peg is generation >= 1 of the SAME maker_limit execution: the internal
cl_ord_id stays put, the provider id moves with the generation. Fill identity
comes from the persisted txid and the transition, never from a response field.
"""
import json

from _harness import kr, Runner
from test_repeg_decision import (
    CL, _pid, _recovery_row, _run_recovery_cycle, _run_cancel_open_readback_race,
)

LEGACY_FIELD = "cl_" + "ordid"
CANCELED = {"status": "canceled", "vol_exec": "0.00000000",
            "cost": "0.00000", "fee": "0.00000", "price": "0.00000"}
DUP = lambda: kr.KrakenError(["EOrder:Duplicate order"])  # noqa: E731


def _cycle(*args, **kwargs):
    """Every ROB-21 re-peg test runs against PRODUCTION-shaped storage: raw is a
    JSON string, never a decoded dict, and a raw-object filter never matches."""
    kwargs.setdefault("string_raw", True)
    return _run_recovery_cycle(*args, **kwargs)


def _adds(trace):
    return [p for e, p in trace["calls"] if e == "AddOrder"]


def _arm_update(trace):
    return next(u for _t, _f, u in trace["state_updates"]
                if u.get("status") == kr.REPEG_RECOVERY_STATUS)


# ── generation identity ───────────────────────────────────────

def t_generation_provider_id_is_deterministic_and_persisted_at_arm(r):
    r.check("generation 1 is a valid provider id", kr.valid_provider_id(_pid(1)), True)
    r.check("generation id is stable", _pid(2), kr.provider_client_id("maker_limit", CL, 2))
    r.check("generations differ", len({_pid(g) for g in range(0, 5)}), 5)

    src = {
        "cl_ord_id": CL, "pair": "KASUSD", "order_id": "O-GEN1", "trade_date_chicago": "2026-09-11",
        "parent_event_id": "evt", "dca_order_id": 1, "execution_started_at": "2026-09-11T11:53:15+00:00",
        "limit_price": 0.03413, "requested_quote_amount_base": 10.0,
        "raw": json.dumps({"repeg_count": 1, "kraken_cl": _pid(1)}),
    }
    trace = _run_cancel_open_readback_race(source_row=src)
    armed = json.loads(_arm_update(trace)["raw"])
    transition = armed["repeg_transition"]
    r.check("generation 2 replacement id", transition["replacement_cl_ord_id"], _pid(2))
    r.check("previous generation provider id retained", transition["original_provider_cl_ord_id"], _pid(1))
    r.check("internal execution id unchanged", transition["original_cl_ord_id"], CL)
    r.check("raw.kraken_cl moves to the new generation at arm", armed["kraken_cl"], _pid(2))
    r.check("generation counter", transition["generation"], 2)
    order = [e for e in trace["trace_events"] if e[1] in (kr.REPEG_RECOVERY_STATUS, "CancelOrder")]
    r.check("replacement id durable before the cancel", order[0], ("db", kr.REPEG_RECOVERY_STATUS))


def t_row_without_a_valid_provider_id_is_not_repegged(r):
    for label, raw in (("none", {"repeg_count": 0}), ("legacy long id", {"repeg_count": 0, "kraken_cl": CL}),
                       ("garbage", {"repeg_count": 0, "kraken_cl": "x"})):
        src = {
            "cl_ord_id": CL, "pair": "KASUSD", "order_id": "O1", "trade_date_chicago": "2026-09-11",
            "parent_event_id": "evt", "dca_order_id": 1, "execution_started_at": "2026-09-11T11:53:15+00:00",
            "limit_price": 0.03413, "requested_quote_amount_base": 10.0, "raw": json.dumps(raw),
        }
        trace = _run_cancel_open_readback_race(source_row=src)
        r.check(f"{label}: not handled", trace["handled"], False)
        r.check(f"{label}: no Kraken call (no cancel, no AddOrder)", trace["calls"], [])
        r.check(f"{label}: no state write", trace["state_updates"], [])
        r.check(f"{label}: no fallback to the internal id", any(CL in json.dumps(c) for c in trace["calls"]), False)


# ── fill identity from persisted txid ─────────────────────────

def _transition():
    t = _recovery_row("replacement_attached")["raw"]["repeg_transition"]
    t["original_order_id"] = "O-ORIG"
    t["replacement_order_id"] = "O-REPL"
    t["generation"] = 1
    return t


def t_fill_generation_identified_by_txid_not_response(r):
    obs = {"status": "canceled", "vol_exec": "1.0", "cost": "0.03", "fee": "0.0001", "price": "0.03"}

    t = _transition()
    r.check("original txid accepted", kr._repeg_apply_generation_fill(t, dict(obs), "O-ORIG"), True)
    entry = t["generation_fills"][0]
    r.check("original charges generation 0", entry["generation"], 0)
    r.check("original provider id comes from the transition", entry["provider_client_id"], _pid(0))
    r.check("original provider txid", entry["provider_order_id"], "O-ORIG")

    t = _transition()
    r.check("replacement txid accepted", kr._repeg_apply_generation_fill(t, dict(obs), "O-REPL"), True)
    entry = t["generation_fills"][0]
    r.check("replacement charges generation 1", entry["generation"], 1)
    r.check("replacement provider id comes from the transition", entry["provider_client_id"], _pid(1))

    # A response field that disagrees must not move the charge between generations.
    t = _transition()
    misleading = {**obs, "cl_ord_id": _pid(1)}
    kr._repeg_apply_generation_fill(t, misleading, "O-ORIG")
    r.check("response client id cannot reassign the generation", t["generation_fills"][0]["generation"], 0)

    t = _transition()
    foreign = {**obs, "cl_ord_id": "dca1-zzzzzzzzzzzzz", LEGACY_FIELD: _pid(1)}
    kr._repeg_apply_generation_fill(t, foreign, "O-REPL")
    r.check("foreign response ids are ignored", t["generation_fills"][0]["generation"], 1)

    for label, oid in (("unknown txid", "O-OTHER"), ("no txid", None), ("empty txid", "")):
        t = _transition()
        r.check(f"{label}: fail closed", kr._repeg_apply_generation_fill(t, dict(obs), oid), False)
        r.check(f"{label}: nothing recorded", t.get("generation_fills"), [])


# ── replacement submission ────────────────────────────────────

def t_replacement_addorder_uses_generation_id_and_canonical_field(r):
    trace = _cycle(CANCELED, row=_recovery_row("original_terminal"))
    adds = _adds(trace)
    r.check("one AddOrder", len(adds), 1)
    r.check("generation provider id", adds[0]["cl_ord_id"], _pid(1))
    r.check("no legacy field", LEGACY_FIELD in adds[0], False)
    r.check("only the seven replacement fields are sent", sorted(adds[0]),
            ["cl_ord_id", "oflags", "ordertype", "pair", "price", "type", "volume"])


def t_legacy_persisted_request_field_is_never_sent(r):
    row = _recovery_row("original_terminal")
    request = row["raw"]["repeg_transition"]["replacement_request"]
    request[LEGACY_FIELD] = request.pop("cl_ord_id")   # a request persisted before ROB-21
    trace = _cycle(CANCELED, row=row)
    adds = _adds(trace)
    r.check("AddOrder still goes out once", len(adds), 1)
    r.check("legacy field stripped", LEGACY_FIELD in adds[0], False)
    r.check("canonical field carries the provider id", adds[0]["cl_ord_id"], _pid(1))


def t_replacement_with_invalid_or_unpersisted_id_fails_closed(r):
    legacy = _recovery_row("original_terminal")
    legacy["raw"]["repeg_transition"]["replacement_cl_ord_id"] = CL + "-r1"
    legacy["raw"]["kraken_cl"] = CL + "-r1"
    trace = _cycle(CANCELED, row=legacy)
    r.check("legacy replacement id: no AddOrder", _adds(trace), [])
    r.check("legacy replacement id: manual", trace["row"]["status"], "manual_required")

    mismatch = _recovery_row("original_terminal")
    mismatch["raw"]["kraken_cl"] = _pid(2)           # persisted id is not the transition's
    trace = _cycle(CANCELED, row=mismatch)
    r.check("persisted id differs from the transition: no AddOrder", _adds(trace), [])
    r.check("persisted id differs from the transition: manual", trace["row"]["status"], "manual_required")

    missing = _recovery_row("original_terminal")
    missing["raw"].pop("kraken_cl")
    trace = _cycle(CANCELED, row=missing)
    r.check("no persisted id: no AddOrder", _adds(trace), [])
    r.check("no persisted id: manual", trace["row"]["status"], "manual_required")


# ── ambiguity and duplicates ──────────────────────────────────

def t_response_loss_never_blind_resubmits_across_many_cycles(r):
    first = _cycle(CANCELED, row=_recovery_row("original_terminal"),
                                add_order_error=TimeoutError("response lost"))
    r.check("attempted exactly once", len(_adds(first)), 1)
    row = first["row"]
    total = len(_adds(first))
    for _ in range(4):
        again = _cycle(CANCELED, row=row)
        total += len(_adds(again))
        row = again["row"]
    r.check("the same provider id never reaches AddOrder twice", total, 1)
    r.check("still non-terminal", row["status"], kr.REPEG_RECOVERY_STATUS)


def t_ambiguity_lookup_uses_canonical_filter_and_row_start(r):
    pending = _recovery_row("replacement_submission_pending")
    trace = _cycle(row=pending)
    opened = [p for e, p in trace["calls"] if e == "OpenOrders"]
    closed = [p for e, p in trace["calls"] if e == "ClosedOrders"]
    r.check("open filter", opened[0], {"cl_ord_id": _pid(1)})
    r.check("closed filter", closed[0]["cl_ord_id"], _pid(1))
    r.check("closed lookup is bounded and cursor-driven", (closed[0]["with_cursor"], closed[0]["closetime"]), ("true", "close"))
    started = kr._lookup_started_at(pending["execution_started_at"])
    r.check("closed start derives from execution_started_at",
            closed[0]["start"], str(int((started - kr.LOOKUP_START_MARGIN).timestamp())))
    r.check("no AddOrder from the ambiguity owner", _adds(trace), [])
    r.check("ABSENT stays pending, silently", (trace["row"]["status"], trace["tg_calls"]), (kr.REPEG_RECOVERY_STATUS, []))


def t_replacement_duplicate_rejection(r):
    found = _cycle(
        CANCELED, row=_recovery_row("original_terminal"), add_order_error=DUP(),
        replacement_open={"ODUP": {"status": "open", "vol_exec": "0.00000000", "cl_ord_id": _pid(1)}})
    r.check("duplicate + FOUND attaches", (found["row"]["status"], found["row"]["order_id"]), ("limit_open", "ODUP"))
    r.check("duplicate + FOUND: one AddOrder", len(_adds(found)), 1)
    r.check("duplicate + FOUND: provider id kept", found["row"]["raw"]["kraken_cl"], _pid(1))

    absent = _cycle(CANCELED, row=_recovery_row("original_terminal"), add_order_error=DUP())
    r.check("duplicate + ABSENT: still recovery pending", absent["row"]["status"], kr.REPEG_RECOVERY_STATUS)
    r.check("duplicate + ABSENT: not rejected", absent["row"]["status"] != "rejected_postonly", True)
    r.check("duplicate + ABSENT: no fallback", absent["fallback_calls"], [])
    r.check("duplicate + ABSENT: alerted", len(absent["tg_calls"]), 1)
    r.check("duplicate + ABSENT: one AddOrder", len(_adds(absent)), 1)

    foreign = {"OX": {"status": "open", "vol_exec": "0", "cl_ord_id": "dca1-zzzzzzzzzzzzz"}}
    unknown = _cycle(CANCELED, row=_recovery_row("original_terminal"), add_order_error=DUP(),
                                  replacement_open=foreign)
    r.check("duplicate + UNKNOWN: still recovery pending", unknown["row"]["status"], kr.REPEG_RECOVERY_STATUS)
    r.check("duplicate + UNKNOWN: no fallback", unknown["fallback_calls"], [])
    r.check("duplicate + UNKNOWN: alerted", len(unknown["tg_calls"]), 1)
    r.check("duplicate + UNKNOWN: one AddOrder", len(_adds(unknown)), 1)



# ── manual escalation ownership (production-shaped raw) ───────

def _unresolved(**kw):
    """Original provider order unresolved, evaluated just past the frozen TTL."""
    row = kw.pop("row", None) or _recovery_row()
    manual_at = kr._repeg_time(row["raw"]["repeg_transition"]["manual_at"])
    return _cycle(None, row=row, now=manual_at + kr.timedelta(seconds=1), **kw)


def _manual_writes(trace):
    return [u for _t, _f, u in trace["updates"] if u.get("status") == "manual_required"]


def t_store_is_production_shaped(r):
    trace = _cycle(CANCELED, row=_recovery_row("original_terminal"))
    r.check("raw stayed a JSON string in the store", trace["stored_raw_is_string"], True)
    r.check("decoded view is available to logic", isinstance(trace["row"]["raw"], dict), True)
    r.check("no write ever carried a raw-equality filter",
            [f for _t, f, _u in trace["updates"] if "raw" in f], [])
    # The harness is faithful: a raw-object filter cannot match a string-stored row.
    probe = _cycle(None, row=_recovery_row())
    r.check("harness stored raw as a string for the probe too", probe["stored_raw_is_string"], True)


def t_manual_escalation_persists_with_string_stored_raw(r):
    trace = _unresolved()
    r.check("manual_required is written", trace["row"]["status"], "manual_required")
    r.check("note is persisted", "unresolved at frozen TTL" in trace["row"]["reason"], True)
    r.check("exactly one manual write", len(_manual_writes(trace)), 1)
    r.check("normal manual alert emitted once", len(trace["tg_calls"]), 1)
    r.check("it is the normal alert", ("MANUAL REQUIRED" in trace["tg_calls"][0]
                                       and "NOT PERSISTED" not in trace["tg_calls"][0]), True)
    r.check("store still holds a string", trace["stored_raw_is_string"], True)
    r.check("provider id preserved", trace["row"]["raw"]["kraken_cl"], _pid(1))
    r.check("no AddOrder", _adds(trace), [])
    guarded = next(f for _t, f, u in trace["updates"] if u.get("status") == "manual_required")
    r.check("ownership predicate: status", guarded["status"], f"eq.{kr.REPEG_RECOVERY_STATUS}")
    r.check("ownership predicate: provider order", guarded["order_id"], "eq.O756Z5-SM7WB-5IAX7D")
    r.check("ownership predicate: internal id", guarded["cl_ord_id"], f"eq.{CL}")
    r.check("no raw-equality filter", "raw" in guarded, False)


def _assert_not_persisted(r, label, trace):
    # `updates` logs every ATTEMPTED write, including ones that matched no row,
    # so "not written" is asserted on the STORE; attempts are asserted per case.
    r.check(f"{label}: manual_required NOT in the store",
            trace["row"]["status"] != "manual_required", True)
    r.check(f"{label}: exactly one alert", len(trace["tg_calls"]), 1)
    r.check(f"{label}: it is the DISTINCT failure-to-persist alert",
            ("NOT PERSISTED" in trace["tg_calls"][0] and "ownership/CAS no longer matched" in trace["tg_calls"][0]), True)
    r.check(f"{label}: it is not the normal manual alert", "DCA MANUAL REQUIRED" in trace["tg_calls"][0], False)
    r.check(f"{label}: no AddOrder", _adds(trace), [])
    r.check(f"{label}: no fallback", trace["fallback_calls"], [])


def t_manual_escalation_cas_miss_is_alerted_not_written(r):
    # Ownership lost AT the guarded update (a concurrent owner advanced the row).
    trace = _unresolved(advance_before_manual=True)
    _assert_not_persisted(r, "CAS miss", trace)
    r.check("CAS miss: the guarded update WAS attempted once and missed", len(_manual_writes(trace)), 1)
    r.check("the concurrent owner's state is retained", trace["row"]["status"], "limit_open")

    # The guarded update matches no row but nothing changed: row stays in recovery.
    trace = _unresolved(manual_cas_miss=True)
    _assert_not_persisted(r, "CAS miss (row unchanged)", trace)
    r.check("CAS miss (row unchanged): attempted once and missed", len(_manual_writes(trace)), 1)
    r.check("row stays in recovery", trace["row"]["status"], kr.REPEG_RECOVERY_STATUS)

    # Repetition while the row stays in recovery is accepted (no suppression state).
    first = _unresolved(manual_cas_miss=True)
    second = _cycle(None, row=first["row"], manual_cas_miss=True,
                    now=kr._repeg_time(first["row"]["raw"]["repeg_transition"]["manual_at"]) + kr.timedelta(seconds=1))
    r.check("a persisting miss re-alerts on the next cycle", len(second["tg_calls"]), 1)
    r.check("and still never spends", _adds(first) + _adds(second), [])


def t_manual_escalation_validates_decoded_ownership_before_updating(r):
    def drift_phase(db_row):
        raw = json.loads(db_row["raw"])
        raw["repeg_transition"]["phase"] = "cancel_pending"
        db_row["raw"] = json.dumps(raw)

    def drift_status(db_row):
        db_row["status"] = "limit_open"

    def drift_generation(db_row):
        raw = json.loads(db_row["raw"])
        raw["repeg_transition"]["generation"] = 2
        db_row["raw"] = json.dumps(raw)

    def drift_order(db_row):
        db_row["order_id"] = "O-OTHER"

    def corrupt(db_row):
        db_row["raw"] = "{not json"

    def not_an_object(db_row):
        db_row["raw"] = json.dumps(["x"])

    def no_transition(db_row):
        db_row["raw"] = json.dumps({"kraken_cl": _pid(1)})

    def unreadable(_db_row):
        raise RuntimeError("PostgREST unavailable")

    cases = (
        ("phase changed", drift_phase, "phase changed"),
        ("status changed", drift_status, "status is now"),
        ("generation changed", drift_generation, "generation changed"),
        ("provider order changed", drift_order, "provider order changed"),
        ("raw corrupt", corrupt, "not decodable"),
        ("raw not an object", not_an_object, "not decodable"),
        ("transition missing", no_transition, "appeared or disappeared"),
        ("re-read fails", unreadable, "re-read failed"),
    )
    for label, hook, reason in cases:
        trace = _unresolved(reread_hook=hook)
        _assert_not_persisted(r, label, trace)
        r.check(f"{label}: no update was even attempted", _manual_writes(trace), [])
        # The REASON, not just the outcome: each guard is tested on its own.
        r.check(f"{label}: alert names the reason", reason in trace["tg_calls"][0], True)


def t_manual_escalation_covers_every_caller_with_string_raw(r):
    # Each in-recovery dead-letter exit, on production-shaped storage.
    legacy = _recovery_row("original_terminal")
    legacy["raw"]["repeg_transition"]["replacement_cl_ord_id"] = CL + "-r1"
    legacy["raw"]["kraken_cl"] = CL + "-r1"
    trace = _cycle(CANCELED, row=legacy)
    r.check("invalid replacement id: manual_required persisted", trace["row"]["status"], "manual_required")
    r.check("invalid replacement id: one normal alert", len(trace["tg_calls"]), 1)
    r.check("invalid replacement id: no AddOrder", _adds(trace), [])

    mismatch = _recovery_row("original_terminal")
    mismatch["raw"]["kraken_cl"] = _pid(2)
    trace = _cycle(CANCELED, row=mismatch)
    r.check("unpersisted replacement id: manual_required persisted", trace["row"]["status"], "manual_required")
    r.check("unpersisted replacement id: no AddOrder", _adds(trace), [])

    broken = _recovery_row()
    broken["raw"].pop("repeg_transition")
    trace = _cycle(None, row=broken)
    r.check("missing envelope: manual_required persisted", trace["row"]["status"], "manual_required")

    # Ownership miss on the same exit: distinct alert, nothing written.
    miss = _cycle(CANCELED, row=legacy, manual_cas_miss=True)
    r.check("invalid id + CAS miss: not in the store", miss["row"]["status"] != "manual_required", True)
    r.check("invalid id + CAS miss: attempted once and missed", len(_manual_writes(miss)), 1)
    r.check("invalid id + CAS miss: distinct alert", "NOT PERSISTED" in miss["tg_calls"][0], True)
    r.check("invalid id + CAS miss: no AddOrder", _adds(miss), [])


TESTS = [
    ("generation id deterministic, persisted at arm", t_generation_provider_id_is_deterministic_and_persisted_at_arm),
    ("no valid provider id => no re-peg", t_row_without_a_valid_provider_id_is_not_repegged),
    ("fill identity from txid, not response", t_fill_generation_identified_by_txid_not_response),
    ("replacement uses generation id + canonical field", t_replacement_addorder_uses_generation_id_and_canonical_field),
    ("legacy persisted request field never sent", t_legacy_persisted_request_field_is_never_sent),
    ("invalid/unpersisted replacement id fails closed", t_replacement_with_invalid_or_unpersisted_id_fails_closed),
    ("response loss never resubmits", t_response_loss_never_blind_resubmits_across_many_cycles),
    ("ambiguity lookup filter and start", t_ambiguity_lookup_uses_canonical_filter_and_row_start),
    ("replacement duplicate rejection", t_replacement_duplicate_rejection),
    ("store is production-shaped", t_store_is_production_shaped),
    ("manual escalation persists with string raw", t_manual_escalation_persists_with_string_stored_raw),
    ("manual escalation CAS miss is alerted, not written", t_manual_escalation_cas_miss_is_alerted_not_written),
    ("manual escalation validates decoded ownership first", t_manual_escalation_validates_decoded_ownership_before_updating),
    ("every dead-letter exit works with string raw", t_manual_escalation_covers_every_caller_with_string_raw),
]

if __name__ == "__main__":
    raise SystemExit(Runner("ROB-21 re-peg identity").run(TESTS))
