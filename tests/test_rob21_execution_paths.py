#!/usr/bin/env python3
"""ROB-21: the buy path, end to end, against an in-memory Supabase + Kraken.

Covers: provider id on every AddOrder, persisted BEFORE it, `raw.kraken_cl`
preserved by every other write, duplicate rejection handling, at-most-once,
ROB-18 takeover identity, and stale reconciliation by persisted provider id.
Nothing here touches a network or a credential.
"""
import json

from _harness import kr, Runner
from _rob21_world import (
    World, MAKER_SETTINGS, MARKET_SETTINGS, ORDER, TODAY, CL,
)

PID_MAKER = kr.provider_client_id("maker_limit", CL, 0)        # dca1-fouaokqnnmxdu
PID_MARKET = kr.provider_client_id("market", CL, 0)            # dca1-jpc6cy7a5vp6c
FB_CL = CL + "-fb"
PID_FB = kr.provider_client_id("maker_fallback", FB_CL, 0)     # dca1-7hp47c5m4tcfy
REAL_FINALIZE = kr.finalize_order      # captured before any world replaces it
DUPLICATE = lambda: kr.KrakenError(["EOrder:Duplicate order"])  # noqa: E731
INSUFFICIENT = lambda: kr.KrakenError(["EOrder:Insufficient funds"])  # noqa: E731


def run_pair(world, settings=MAKER_SETTINGS, **kwargs):
    with world.installed():
        return kr.execute_pair(dict(ORDER), dict(settings), TODAY, "test-user", **kwargs)


def stale_started(minutes=20):
    return (kr.datetime.now(kr.timezone.utc) - kr.timedelta(minutes=minutes)).isoformat()


def seed_claimed(world, cl, attempt, pid, started=None, **extra):
    raw = {"kraken_cl": pid} if pid else {}
    raw.update(extra.pop("raw", {}))
    return world.add_row(
        cl_ord_id=cl, status="claimed", attempt_type=attempt, order_id=None,
        execution_started_at=started or stale_started(), raw=raw or None,
        parent_event_id="evt-1", dca_order_id=1, **extra)


def seed_maker_for_fallback(world):
    return world.add_row(
        cl_ord_id=CL, status="canceled_unfilled", attempt_type="maker_limit", order_id="OMAKER",
        reason=None, parent_event_id="evt-1", dca_order_id=1, requested_quote_amount_base=10.0,
        filled_quote_cost=0.0, fee_quote=0.0, execution_started_at=stale_started(40),
        raw={"kraken_cl": PID_MAKER})


def run_fallback(world, error=None, after=None):
    maker = seed_maker_for_fallback(world)
    if error is not None:
        def boom(_params):
            raise error
        world.add_order = boom
    window_end = kr.datetime.now(kr.CHICAGO_TZ) + kr.timedelta(minutes=30)
    with world.installed():
        kr._fallback_decision(dict(maker), dict(MAKER_SETTINGS), "test-user", window_end,
                              dry_run=False, scenario=None)
    return maker


def first_index(world, predicate):
    return next(i for i, e in enumerate(world.events) if predicate(e))


# ── provider id on AddOrder, persisted first ──────────────────

def t_maker_addorder_uses_provider_id_persisted_first(r):
    w = World()
    out = run_pair(w)
    add = w.calls("AddOrder")
    r.check("one AddOrder", len(add), 1)
    r.check("maker AddOrder cl_ord_id is the provider id", add[0]["cl_ord_id"], PID_MAKER)
    r.check("known vector", PID_MAKER, "dca1-fouaokqnnmxdu")
    r.check("claim row persisted the provider id", json.loads(w.rows[0]["raw"])["kraken_cl"], PID_MAKER)
    r.check("provider id persisted BEFORE AddOrder",
            first_index(w, lambda e: e[:2] == ("db", "insert")) < first_index(w, lambda e: e == ("kraken", "AddOrder")), True)
    r.check("maker success preserves raw.kraken_cl", w.raw(CL)["kraken_cl"], PID_MAKER)
    r.check("maker success status", (w.row(CL)["status"], out["status"]), ("limit_open", "limit_open"))
    r.check("provider response is still recorded", "txid" in w.raw(CL), True)
    r.check("internal id is untouched", w.row(CL)["cl_ord_id"], CL)


def t_market_addorder_uses_provider_id_persisted_first(r):
    w = World()
    run_pair(w, MARKET_SETTINGS)
    add = w.calls("AddOrder")
    r.check("one AddOrder", len(add), 1)
    r.check("market AddOrder cl_ord_id is the provider id", add[0]["cl_ord_id"], PID_MARKET)
    r.check("known vector", PID_MARKET, "dca1-jpc6cy7a5vp6c")
    r.check("provider id persisted BEFORE AddOrder",
            first_index(w, lambda e: e[:2] == ("db", "insert")) < first_index(w, lambda e: e == ("kraken", "AddOrder")), True)
    r.check("market success preserves raw.kraken_cl", w.raw(CL)["kraken_cl"], PID_MARKET)
    r.check("market placed", w.row(CL)["status"], "placed")
    r.check("market finalized by txid", w.finalized, [(CL, "OTXID-NEW")])


def t_fallback_addorder_uses_provider_id_persisted_first(r):
    w = World()
    run_fallback(w)
    add = w.calls("AddOrder")
    r.check("one AddOrder", len(add), 1)
    r.check("fallback AddOrder cl_ord_id is the provider id", add[0]["cl_ord_id"], PID_FB)
    r.check("known vector", PID_FB, "dca1-7hp47c5m4tcfy")
    r.check("fallback claim persisted the provider id", json.loads(w.row(FB_CL)["raw"])["kraken_cl"], PID_FB)
    r.check("provider id persisted BEFORE AddOrder",
            first_index(w, lambda e: e == ("db", "insert", FB_CL)) < first_index(w, lambda e: e == ("kraken", "AddOrder")), True)
    r.check("fallback success preserves raw.kraken_cl", w.raw(FB_CL)["kraken_cl"], PID_FB)
    r.check("fallback placed", w.row(FB_CL)["status"], "placed")
    r.check("maker event resolved", w.row(CL)["reason"], "fallback_created")


def t_retry_takeover_gets_a_new_provider_id_atomically(r):
    w = World()
    w.add_row(id=7, cl_ord_id=CL, status="failed_kraken", attempt_type="maker_limit", dca_order_id=1,
              reason="limit AddOrder failed: ['EOrder:Insufficient funds']", parent_event_id="evt-1",
              execution_started_at=stale_started(30),
              raw={"error": "['EOrder:Insufficient funds']", "kraken_cl": PID_MAKER})
    run_pair(w)
    new_cl = CL + "-r1"
    new_pid = kr.provider_client_id("maker_limit", new_cl, 0)
    r.check("takeover rotates the internal id (ROB-18 unchanged)", w.rows[0]["cl_ord_id"], new_cl)
    r.check("new provider id differs from the previous one", new_pid != PID_MAKER, True)
    add = w.calls("AddOrder")
    r.check("exactly one AddOrder after takeover", len(add), 1)
    r.check("takeover AddOrder uses the NEW provider id", add[0]["cl_ord_id"], new_pid)
    r.check("previous provider id is never sent", all(p.get("cl_ord_id") != PID_MAKER for p in add), True)
    rotate = next(e for e in w.events if e[:2] == ("db", "update") and "cl_ord_id" in e[2])
    r.check("rotation and provider id persist in the SAME update", "raw" in rotate[2], True)
    r.check("persisted before AddOrder",
            w.events.index(rotate) < first_index(w, lambda e: e == ("kraken", "AddOrder")), True)
    r.check("final raw carries the new provider id", w.raw(new_cl)["kraken_cl"], new_pid)


def t_takeover_not_taken_for_unlisted_errors(r):
    w = World()
    w.add_row(id=7, cl_ord_id=CL, status="failed_kraken", attempt_type="maker_limit", dca_order_id=1,
              reason="limit AddOrder failed: ['EOrder:Something else']", parent_event_id="evt-1",
              execution_started_at=stale_started(30), raw={"kraken_cl": PID_MAKER})
    out = run_pair(w)
    r.check("ROB-18 eligibility unchanged: no takeover", out.get("status"), "already_claimed")
    r.check("no AddOrder at all", w.calls("AddOrder"), [])
    r.check("row untouched", w.rows[0]["cl_ord_id"], CL)


# ── other writes preserve raw.kraken_cl ───────────────────────

def t_failure_paths_preserve_provider_id(r):
    w = World()
    w.add_order = lambda _p: (_ for _ in ()).throw(INSUFFICIENT())
    run_pair(w)
    r.check("maker failure status", w.row(CL)["status"], "failed_kraken")
    r.check("maker failure preserves raw.kraken_cl", w.raw(CL)["kraken_cl"], PID_MAKER)
    r.check("failure record keeps the request", w.raw(CL)["request"]["cl_ord_id"], PID_MAKER)

    w = World()
    w.add_order = lambda _p: (_ for _ in ()).throw(INSUFFICIENT())
    run_pair(w, MARKET_SETTINGS)
    r.check("market failure preserves raw.kraken_cl", w.raw(CL)["kraken_cl"], PID_MARKET)

    w = World()
    w.add_order = lambda _p: (_ for _ in ()).throw(kr.KrakenError(["EOrder:Post only order"]))
    original = kr._fallback_decision
    w_calls = []
    kr._fallback_decision = lambda *a, **k: w_calls.append(a)
    try:
        run_pair(w)
    finally:
        kr._fallback_decision = original
    r.check("post-only reject status", w.row(CL)["status"], "rejected_postonly")
    r.check("post-only reject preserves raw.kraken_cl", w.raw(CL)["kraken_cl"], PID_MAKER)

    w = World()
    run_fallback(w, error=INSUFFICIENT())
    r.check("fallback failure status", w.row(FB_CL)["status"], "failed_kraken")
    r.check("fallback failure preserves raw.kraken_cl", w.raw(FB_CL)["kraken_cl"], PID_FB)

    w = World()
    w.balance = (1.0, 0.0, "fake")
    run_pair(w)
    r.check("insufficient-funds skip status", w.row(CL)["status"], "skipped_insufficient_funds")
    r.check("skip preserves raw.kraken_cl", w.raw(CL)["kraken_cl"], PID_MAKER)
    r.check("a skip made no AddOrder", w.calls("AddOrder"), [])


def t_provider_response_cannot_displace_provider_id(r):
    w = World()
    w.add_order = lambda _p: {"txid": ["OTXID-NEW"], "kraken_cl": "dca1-zzzzzzzzzzzzz"}
    run_pair(w)
    r.check("a provider key named kraken_cl cannot overwrite the id", w.raw(CL)["kraken_cl"], PID_MAKER)


def t_finalize_merge_preserves_provider_id(r):
    # The REAL finalize_order (captured before the world replaces it): its merge
    # must keep the persisted provider id even when the provider response
    # carries a key of the same name.
    w = World()
    w.add_row(cl_ord_id=CL, status="placed", attempt_type="market", order_id="OT",
              execution_started_at=stale_started(), raw={"kraken_cl": PID_MARKET, "repeg_count": 0})
    w.closed_orders["OT"] = {
        "status": "closed", "vol_exec": "10", "cost": "0.34", "fee": "0.003", "price": "0.034",
        "cl_ord_id": "dca1-zzzzzzzzzzzzz", "kraken_cl": "hijack", "closetm": 1759766400.0,
    }
    quiet = {
        "get_ohlc_ctx": lambda *_a, **_k: {},
        "resolve_reference_mid": lambda *_a, **_k: (None, None, None),
        "_remaining_after_buy_line": lambda: "",
    }
    saved = {name: getattr(kr, name) for name in quiet}
    with w.installed():
        for name, fn in quiet.items():
            setattr(kr, name, fn)
        try:
            REAL_FINALIZE(CL, "OT")
        finally:
            for name, fn in saved.items():
                setattr(kr, name, fn)
    r.check("finalize wrote the fill", w.row(CL)["status"], "filled")
    r.check("finalize merge keeps the persisted provider id", w.raw(CL)["kraken_cl"], PID_MARKET)
    r.check("finalize merge keeps other persisted keys", w.raw(CL)["repeg_count"], 0)


def t_takeover_raw_never_carries_the_previous_identitys_state(r):
    w = World()
    stale = {"kraken_cl": PID_MAKER, "error": "['EOrder:Insufficient funds']", "at": "2026-10-06T12:00:00+00:00",
             "request": {"cl_ord_id": PID_MAKER}, "repeg_count": 3,
             "repeg_transition": {"phase": "replacement_attached", "generation_fills": [{"generation": 0}]}}
    w.add_row(id=7, cl_ord_id=CL, status="failed_kraken", attempt_type="maker_limit", dca_order_id=1,
              reason="limit AddOrder failed: ['EOrder:Insufficient funds']", parent_event_id="evt-1",
              execution_started_at=stale_started(30), raw=stale)
    new_cl = CL + "-r1"
    new_pid = kr.provider_client_id("maker_limit", new_cl, 0)
    _ambiguous_add(w, new_pid)           # crash right after AddOrder: row keeps its takeover raw
    try:
        run_pair(w)
    except TimeoutError:
        pass
    raw = w.raw(new_cl)
    r.check("takeover raw holds only the new identity and a namespaced summary",
            sorted(raw), ["kraken_cl", "previous_attempt"])
    r.check("new provider id", raw["kraken_cl"], new_pid)
    r.check("summary names the previous internal id", raw["previous_attempt"]["cl_ord_id"], CL)
    r.check("summary keeps the previous error", raw["previous_attempt"]["error"], "['EOrder:Insufficient funds']")
    r.check("counters and fills do not cross identities", "repeg_count" in raw or "repeg_transition" in raw, False)
    r.check("the previous provider id is not at the top level", raw["kraken_cl"] != PID_MAKER, True)

    # And a SUCCESSFUL takeover merges onto that base rather than replacing it.
    w = World()
    w.add_row(id=7, cl_ord_id=CL, status="failed_kraken", attempt_type="maker_limit", dca_order_id=1,
              reason="limit AddOrder failed: ['EOrder:Insufficient funds']", parent_event_id="evt-1",
              execution_started_at=stale_started(30), raw=stale)
    run_pair(w)
    final = w.raw(new_cl)
    r.check("success keeps the new provider id", final["kraken_cl"], new_pid)
    r.check("success merges the provider result", final["txid"], ["OTXID-NEW"])
    r.check("success keeps the takeover summary", final["previous_attempt"]["cl_ord_id"], CL)
    r.check("success still carries no previous-identity counters", "repeg_count" in final, False)


def t_writes_merge_what_the_run_already_persisted(r):
    w = World()
    w.add_order = lambda _p: (_ for _ in ()).throw(INSUFFICIENT())
    run_pair(w)
    raw = w.raw(CL)
    r.check("failure record merges onto the claim base", raw["kraken_cl"], PID_MAKER)
    r.check("failure record keeps the request", raw["request"]["pair"], "KASUSD")
    r.check("failure record keeps the preflight reading", raw["balance_source"], "fake")
    w = World()
    run_pair(w)
    raw = w.raw(CL)
    r.check("success merges result, preflight and provider id",
            (raw["txid"], raw["preflight"]["balance_source"], raw["kraken_cl"]), (["OTXID-NEW"], "fake", PID_MAKER))


# ── duplicate rejection ───────────────────────────────────────

def _dup_world(found: bool, unknown: bool, pid):
    w = World()
    w.add_order = lambda _p: (_ for _ in ()).throw(DUPLICATE())
    if found:
        w.open_orders["ODUP"] = {"status": "open", "vol_exec": "0.0", "cl_ord_id": pid}
    if unknown:
        w.endpoint_errors["OpenOrders"] = RuntimeError("OpenOrders down")
    return w


def t_duplicate_found_recovers(r):
    w = _dup_world(True, False, PID_MAKER)
    out = run_pair(w)
    r.check("maker: duplicate + FOUND attaches the txid", (w.row(CL)["status"], w.row(CL)["order_id"]), ("limit_open", "ODUP"))
    r.check("maker: provider id kept", w.raw(CL)["kraken_cl"], PID_MAKER)
    r.check("maker: AddOrder attempted once", len(w.calls("AddOrder")), 1)
    r.check("maker: result", out["status"], "limit_open")

    w = _dup_world(True, False, PID_MARKET)
    run_pair(w, MARKET_SETTINGS)
    r.check("market: duplicate + FOUND attaches the txid", (w.row(CL)["status"], w.row(CL)["order_id"]), ("placed", "ODUP"))
    r.check("market: finalized by the recovered txid", w.finalized, [(CL, "ODUP")])
    r.check("market: AddOrder attempted once", len(w.calls("AddOrder")), 1)

    w = _dup_world(True, False, PID_FB)
    run_fallback(w, error=DUPLICATE())
    r.check("fallback: duplicate + FOUND attaches the txid", (w.row(FB_CL)["status"], w.row(FB_CL)["order_id"]), ("placed", "ODUP"))
    r.check("fallback: provider id kept", w.raw(FB_CL)["kraken_cl"], PID_FB)
    r.check("fallback: finalized by the recovered txid", w.finalized, [(FB_CL, "ODUP")])
    r.check("fallback: event resolved, not failed", w.row(CL)["reason"], "fallback_created")
    r.check("fallback: AddOrder attempted once", len(w.calls("AddOrder")), 1)


def t_duplicate_absent_or_unknown_stays_non_terminal(r):
    for label, unknown in (("ABSENT", False), ("UNKNOWN", True)):
        for kind, settings, pid in (("maker", MAKER_SETTINGS, PID_MAKER), ("market", MARKET_SETTINGS, PID_MARKET)):
            w = _dup_world(False, unknown, pid)
            out = run_pair(w, settings)
            row = w.row(CL)
            r.check(f"{kind} duplicate+{label}: stays claimed", row["status"], "claimed")
            r.check(f"{kind} duplicate+{label}: NOT failed_kraken", row["status"] != "failed_kraken", True)
            r.check(f"{kind} duplicate+{label}: not terminal", row.get("execution_finished_at"), None)
            r.check(f"{kind} duplicate+{label}: submission state UNKNOWN", w.raw(CL)["submission_state"], "UNKNOWN")
            r.check(f"{kind} duplicate+{label}: provider id kept", w.raw(CL)["kraken_cl"], pid)
            r.check(f"{kind} duplicate+{label}: alerted once", len(w.tg), 1)
            r.check(f"{kind} duplicate+{label}: no blind resubmit", len(w.calls("AddOrder")), 1)
            r.check(f"{kind} duplicate+{label}: result", (out["status"], out["submission_state"]), ("claimed", "UNKNOWN"))

        w = _dup_world(False, unknown, PID_FB)
        run_fallback(w, error=DUPLICATE())
        row = w.row(FB_CL)
        r.check(f"fallback duplicate+{label}: stays claimed", row["status"], "claimed")
        r.check(f"fallback duplicate+{label}: NOT failed_kraken", row["status"] != "failed_kraken", True)
        r.check(f"fallback duplicate+{label}: submission state UNKNOWN", w.raw(FB_CL)["submission_state"], "UNKNOWN")
        r.check(f"fallback duplicate+{label}: maker reason is not fallback_failed_kraken",
                w.row(CL)["reason"], "fallback_created")
        r.check(f"fallback duplicate+{label}: no blind resubmit", len(w.calls("AddOrder")), 1)
        r.check(f"fallback duplicate+{label}: alerted", len(w.tg), 1)


# ── at-most-once ──────────────────────────────────────────────

def _ambiguous_add(w, pid):
    def add(params):
        # Kraken accepted the order, the response was lost.
        w.open_orders["OAMBIG"] = {"status": "open", "vol_exec": "0.0", "cl_ord_id": params["cl_ord_id"]}
        raise TimeoutError("response lost")
    w.add_order = add


def t_transport_ambiguity_never_blind_resubmits(r):
    for kind, settings, pid in (("maker", MAKER_SETTINGS, PID_MAKER), ("market", MARKET_SETTINGS, PID_MARKET)):
        w = World()
        _ambiguous_add(w, pid)
        try:
            run_pair(w, settings)
            raised = False
        except TimeoutError:
            raised = True
        r.check(f"{kind}: ambiguity propagates, never becomes failed_kraken", raised, True)
        r.check(f"{kind}: row stays claimed", w.row(CL)["status"], "claimed")
        r.check(f"{kind}: provider id durable", w.raw(CL)["kraken_cl"], pid)
        # Every recovery path, repeated: none may reach AddOrder again.
        again = run_pair(w, settings)
        r.check(f"{kind}: same-day rerun does not resubmit", again.get("status"), "already_claimed")
        w.row(CL)["execution_started_at"] = stale_started()
        with w.installed():
            kr.run_reconciliation("test-user")
            kr.run_reconciliation("test-user")
        r.check(f"{kind}: reconciliation recovers the real order",
                w.row(CL)["order_id"], "OAMBIG")
        r.check(f"{kind}: AddOrder was passed this provider id exactly once", len(w.calls("AddOrder")), 1)


def t_retry_takeover_after_crash_reconciles_by_new_id_only(r):
    w = World()
    new_cl = CL + "-r1"
    new_pid = kr.provider_client_id("maker_limit", new_cl, 0)
    w.add_row(id=7, cl_ord_id=CL, status="failed_kraken", attempt_type="maker_limit", dca_order_id=1,
              reason="limit AddOrder failed: ['EOrder:Insufficient funds']", parent_event_id="evt-1",
              execution_started_at=stale_started(60), raw={"kraken_cl": PID_MAKER})
    # An order exists under the OLD provider id; it must never be consulted.
    w.open_orders["OOLD"] = {"status": "open", "vol_exec": "0.0", "cl_ord_id": PID_MAKER}
    _ambiguous_add(w, new_pid)
    try:
        run_pair(w)
    except TimeoutError:
        pass
    w.row(new_cl)["execution_started_at"] = stale_started()
    before = len(w.kraken_calls)
    with w.installed():
        kr.run_reconciliation("test-user")
    recon = w.kraken_calls[before:]
    filters = {p.get("cl_ord_id") for e, p in recon if e in ("OpenOrders", "ClosedOrders")}
    r.check("reconciliation filtered ONLY by the new provider id", filters, {new_pid})
    r.check("previous provider id never used", PID_MAKER in filters, False)
    r.check("recovered the new order, not the old one", w.row(new_cl)["order_id"], "OAMBIG")
    r.check("takeover attempted AddOrder once", len(w.calls("AddOrder")), 1)


# ── stale reconciliation ──────────────────────────────────────

def _recon(w):
    with w.installed():
        kr.run_reconciliation("test-user")


def t_market_and_fallback_claims_recover_open_and_closed(r):
    for kind, cl, attempt, pid in (("market", CL, "market", PID_MARKET), ("fallback", FB_CL, "maker_fallback", PID_FB)):
        w = World()
        seed_claimed(w, cl, attempt, pid)
        w.open_orders["OOPEN"] = {"status": "open", "vol_exec": "0.0", "cl_ord_id": pid}
        _recon(w)
        r.check(f"{kind} claimed + open recovered", (w.row(cl)["status"], w.row(cl)["order_id"]), ("placed", "OOPEN"))
        r.check(f"{kind} open lookup used the persisted id", {p["cl_ord_id"] for p in w.calls("OpenOrders")}, {pid})
        r.check(f"{kind} recovery never sends AddOrder", w.calls("AddOrder"), [])

        w = World()
        seed_claimed(w, cl, attempt, pid)
        w.closed_orders["OCLOSED"] = {"status": "closed", "vol_exec": "10", "cl_ord_id": pid}
        _recon(w)
        r.check(f"{kind} claimed + closed recovered", w.finalized, [(cl, "OCLOSED")])
        r.check(f"{kind} closed lookup is bounded and cursor-driven",
                (w.calls("ClosedOrders")[0]["with_cursor"], "start" in w.calls("ClosedOrders")[0]), ("true", True))


def t_maker_claim_recovers_open_and_closed(r):
    w = World()
    seed_claimed(w, CL, "maker_limit", PID_MAKER)
    w.open_orders["OOPEN"] = {"status": "open", "vol_exec": "0.0", "cl_ord_id": PID_MAKER}
    _recon(w)
    r.check("maker claimed + open restores limit_open", (w.row(CL)["status"], w.row(CL)["order_id"]), ("limit_open", "OOPEN"))

    w = World()
    seed_claimed(w, CL, "maker_limit", PID_MAKER)
    w.closed_orders["OC"] = {"status": "closed", "vol_exec": "10", "cl_ord_id": PID_MAKER}
    _recon(w)
    r.check("maker claimed + closed finalizes", w.finalized, [(CL, "OC")])


def t_missing_provider_id_fails_closed_without_internal_fallback(r):
    for label, raw in (("missing raw", None), ("raw without kraken_cl", {"x": 1}),
                       ("legacy long id", {"kraken_cl": CL + "-r1"}), ("garbage id", {"kraken_cl": "x"})):
        w = World()
        w.add_row(cl_ord_id=CL, status="claimed", attempt_type="market", order_id=None,
                  execution_started_at=stale_started(), parent_event_id="e", raw=raw)
        # An order that WOULD match the internal id must not be reachable.
        w.open_orders["OTRAP"] = {"status": "open", "vol_exec": "0.0", "cl_ord_id": CL}
        _recon(w)
        r.check(f"{label}: manual_required", w.row(CL)["status"], "manual_required")
        r.check(f"{label}: NOT failed_reconciliation", w.row(CL)["status"] != "failed_reconciliation", True)
        r.check(f"{label}: no lookup was attempted", w.calls("OpenOrders") + w.calls("ClosedOrders"), [])
        r.check(f"{label}: no fresh id was derived or sent", [p for e, p in w.kraken_calls], [])
        r.check(f"{label}: alerted", len(w.tg), 1)


def t_provider_id_of_another_identity_fails_closed(r):
    w = World()
    # A rotated cl_ord_id that still carries the PREVIOUS attempt's provider id.
    seed_claimed(w, CL + "-r1", "maker_limit", PID_MAKER)
    _recon(w)
    r.check("mismatched provider id => manual", w.row(CL + "-r1")["status"], "manual_required")
    r.check("mismatched provider id => no lookup", w.kraken_calls, [])


def t_absent_unknown_and_duplicate_marker_outcomes(r):
    w = World()
    seed_claimed(w, CL, "market", PID_MARKET)
    _recon(w)
    r.check("complete ABSENT keeps the existing outcome", w.row(CL)["status"], "failed_reconciliation")
    r.check("ABSENT never resubmits", w.calls("AddOrder"), [])

    w = World()
    seed_claimed(w, CL, "market", PID_MARKET, raw={"submission_state": "UNKNOWN"})
    _recon(w)
    r.check("ABSENT after a duplicate rejection is manual, not failed", w.row(CL)["status"], "manual_required")

    w = World()
    seed_claimed(w, CL, "market", PID_MARKET)
    w.endpoint_errors["OpenOrders"] = RuntimeError("down")
    _recon(w)
    r.check("UNKNOWN is not ABSENT: row untouched", w.row(CL)["status"], "claimed")
    r.check("UNKNOWN does not alert every cycle", w.tg, [])
    _recon(w)
    r.check("UNKNOWN repeated: still untouched and quiet", (w.row(CL)["status"], w.tg), ("claimed", []))

    w = World()
    seed_claimed(w, CL, "market", PID_MARKET, started=stale_started(kr.LIMIT_TTL_MINUTES + 10))
    w.endpoint_errors["OpenOrders"] = RuntimeError("down")
    _recon(w)
    r.check("UNKNOWN past the frozen TTL escalates once", (w.row(CL)["status"], len(w.tg)), ("manual_required", 1))
    r.check("escalation never resubmits", w.calls("AddOrder"), [])


def t_reconciliation_never_calls_addorder_for_any_state(r):
    w = World()
    seed_claimed(w, CL, "market", PID_MARKET)
    seed_claimed(w, FB_CL, "maker_fallback", PID_FB)
    w.add_row(cl_ord_id=CL + "-x", status="placed", attempt_type="market", order_id="OP", execution_started_at=stale_started())
    _recon(w)
    r.check("no AddOrder from any reconciliation branch", w.calls("AddOrder"), [])


TESTS = [
    ("maker AddOrder: provider id, persisted first", t_maker_addorder_uses_provider_id_persisted_first),
    ("market AddOrder: provider id, persisted first", t_market_addorder_uses_provider_id_persisted_first),
    ("fallback AddOrder: provider id, persisted first", t_fallback_addorder_uses_provider_id_persisted_first),
    ("ROB-18 takeover: new provider id, atomic", t_retry_takeover_gets_a_new_provider_id_atomically),
    ("ROB-18 eligibility unchanged", t_takeover_not_taken_for_unlisted_errors),
    ("failure paths preserve kraken_cl", t_failure_paths_preserve_provider_id),
    ("provider response cannot displace kraken_cl", t_provider_response_cannot_displace_provider_id),
    ("raw merge forces the persisted id last", t_finalize_merge_preserves_provider_id),
    ("takeover raw never carries previous state", t_takeover_raw_never_carries_the_previous_identitys_state),
    ("writes merge onto what the run persisted", t_writes_merge_what_the_run_already_persisted),
    ("duplicate + FOUND recovers", t_duplicate_found_recovers),
    ("duplicate + ABSENT/UNKNOWN non-terminal", t_duplicate_absent_or_unknown_stays_non_terminal),
    ("transport ambiguity never resubmits", t_transport_ambiguity_never_blind_resubmits),
    ("takeover crash reconciles by new id only", t_retry_takeover_after_crash_reconciles_by_new_id_only),
    ("market/fallback claims recover open and closed", t_market_and_fallback_claims_recover_open_and_closed),
    ("maker claims recover open and closed", t_maker_claim_recovers_open_and_closed),
    ("missing provider id fails closed", t_missing_provider_id_fails_closed_without_internal_fallback),
    ("foreign-identity provider id fails closed", t_provider_id_of_another_identity_fails_closed),
    ("ABSENT / UNKNOWN / duplicate-marker outcomes", t_absent_unknown_and_duplicate_marker_outcomes),
    ("reconciliation never sends AddOrder", t_reconciliation_never_calls_addorder_for_any_state),
]

if __name__ == "__main__":
    raise SystemExit(Runner("ROB-21 execution paths").run(TESTS))
