#!/usr/bin/env python3
"""ROB-21: provider client-order identity and the unified provider lookup.

Pure tests: the provider id derivation, its validation, a static scan proving
the legacy client-id field name is gone from the source, and the open+closed
lookup contract (FOUND / ABSENT / UNKNOWN) against scripted Kraken payloads.
"""
import copy
import re
from pathlib import Path

from _harness import kr, Runner

CL = "dca-KASUSD-2026-10-06-704"
FB = "dca-KASUSD-2026-10-06-704-fb"
VECTORS = (
    (("maker_limit", CL, 0), "dca1-fouaokqnnmxdu"),
    (("maker_limit", CL, 1), "dca1-2eyxzolanlvun"),
    (("market", CL, 0), "dca1-jpc6cy7a5vp6c"),
    (("maker_fallback", FB, 0), "dca1-7hp47c5m4tcfy"),
)
PID = "dca1-fouaokqnnmxdu"
STARTED = "2026-10-06T12:00:00+00:00"
NOW = "2026-10-06T12:30:00+00:00"
SRC = Path(__file__).resolve().parent.parent / "src"


# ── provider identity ─────────────────────────────────────────

def t_known_vectors(r):
    for args, expected in VECTORS:
        r.check(f"vector {args[0]} g{args[2]}", kr.provider_client_id(*args), expected)


def t_shape(r):
    for args, expected in VECTORS:
        r.check("18 characters", len(expected), 18)
        r.check("dca1- prefix", expected.startswith("dca1-"), True)
        r.check("derived value has the shape", kr.valid_provider_id(kr.provider_client_id(*args)), True)
        r.check("ASCII lowercase base32 body", re.fullmatch(r"[a-z2-7]{13}", expected[5:]) is not None, True)


def t_distinctness_and_determinism(r):
    a = kr.provider_client_id("maker_limit", CL, 0)
    r.check("deterministic", a, kr.provider_client_id("maker_limit", CL, 0))
    r.check("different internal ids differ", a != kr.provider_client_id("maker_limit", CL + "-r1", 0), True)
    r.check("different generations differ", a != kr.provider_client_id("maker_limit", CL, 1), True)
    r.check("generations 1..5 all differ",
            len({kr.provider_client_id("maker_limit", CL, g) for g in range(0, 6)}), 6)
    kinds = {kr.provider_client_id(t, CL, 0) for t in kr.PROVIDER_ATTEMPT_TYPES}
    r.check("different attempt types differ", len(kinds), 3)


def t_null_and_unknown_attempt_type_derive_nothing(r):
    for bad in (None, "", "limit", "MAKER_LIMIT", "maker", "repeg", 5, ["market"]):
        r.check(f"attempt_type {bad!r} derives no id", kr.provider_client_id(bad, CL, 0), None)


def t_generation_rules(r):
    for kind in ("market", "maker_fallback"):
        r.check(f"{kind} generation 1 rejected", kr.provider_client_id(kind, CL, 1), None)
        r.check(f"{kind} generation 3 rejected", kr.provider_client_id(kind, CL, 3), None)
        r.check(f"{kind} generation 0 allowed", kr.valid_provider_id(kr.provider_client_id(kind, CL, 0)), True)
    r.check("maker_limit generation 1 allowed", kr.valid_provider_id(kr.provider_client_id("maker_limit", CL, 1)), True)
    for bad in (-1, True, False, "1", "01", 1.0, None):
        r.check(f"generation {bad!r} rejected", kr.provider_client_id("maker_limit", CL, bad), None)


def t_missing_internal_id(r):
    for bad in (None, "", 7):
        r.check(f"internal id {bad!r} derives no id", kr.provider_client_id("market", bad, 0), None)


def t_validation_and_raw_extraction(r):
    for good in (v for _a, v in VECTORS):
        r.check("vector is valid", kr.valid_provider_id(good), True)
    for bad in (CL, CL + "-r1", "dca1-FOUAOKQNNMXDU", "dca1-fouaokqnnmxd", "dca1-fouaokqnnmxdu\n",
                "dca1-fouaokqnnmxd1", None, 5, ""):
        r.check(f"{bad!r} is not a provider id", kr.valid_provider_id(bad), False)
    r.check("valid raw.kraken_cl is returned", kr._provider_id_from_raw({"kraken_cl": PID}), PID)
    r.check("missing raw.kraken_cl is None", kr._provider_id_from_raw({}), None)
    r.check("legacy long id is not a provider id", kr._provider_id_from_raw({"kraken_cl": CL + "-r1"}), None)
    r.check("non-dict raw is None", kr._provider_id_from_raw("x"), None)
    r.check("no fallback to the internal id", kr._provider_id_from_raw({"cl_ord_id": CL}), None)


def t_no_legacy_client_id_field_in_source(r):
    legacy = "cl_" + "ordid"
    for path in sorted(SRC.glob("*.py")):
        r.check(f"{path.name} has no legacy client-id literal", legacy in path.read_text(), False)


# ── unified lookup ────────────────────────────────────────────

class Script:
    """Scripted kraken_private: per-endpoint FIFO of results or exceptions."""

    def __init__(self, **by_endpoint):
        self.queue = {k: list(v) for k, v in by_endpoint.items()}
        self.calls = []

    def __call__(self, endpoint, params=None):
        self.calls.append((endpoint, copy.deepcopy(params or {})))
        item = self.queue[endpoint].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def lookup(script, pid=PID, started=STARTED, now=NOW):
    original = kr.kraken_private
    kr.kraken_private = script
    try:
        return kr.kraken_lookup_client_order(pid, started, now)
    finally:
        kr.kraken_private = original


def order(cl=PID, status="open", **extra):
    return {"status": status, "cl_ord_id": cl, **extra}


EMPTY_OPEN = {"open": {}}
EMPTY_CLOSED = {"closed": {}}


def t_open_only_is_found(r):
    res = lookup(Script(OpenOrders=[{"open": {"O1": order()}}], ClosedOrders=[EMPTY_CLOSED]))
    r.check("state", res.state, "FOUND")
    r.check("txid", res.txid, "O1")
    r.check("order returned", res.order["status"], "open")


def t_closed_only_is_found(r):
    res = lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[{"closed": {"C1": order(status="closed")}}]))
    r.check("state", res.state, "FOUND")
    r.check("txid", res.txid, "C1")


def t_same_txid_open_and_closed_dedupes(r):
    res = lookup(Script(OpenOrders=[{"open": {"T": order(status="open")}}],
                        ClosedOrders=[{"closed": {"T": order(status="closed")}}]))
    r.check("race open->closed is one order", res.state, "FOUND")
    r.check("txid", res.txid, "T")
    r.check("the later (closed) observation wins", res.order["status"], "closed")


def t_foreign_order_from_ignored_filter_is_unknown(r):
    for where, script in (
        ("open", Script(OpenOrders=[{"open": {"X": order(cl="dca1-aaaaaaaaaaaaa")}}], ClosedOrders=[EMPTY_CLOSED])),
        ("closed", Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[{"closed": {"X": order(cl="dca1-aaaaaaaaaaaaa")}}])),
        ("mixed", Script(OpenOrders=[{"open": {"O1": order(), "X": order(cl="dca1-aaaaaaaaaaaaa")}}],
                         ClosedOrders=[EMPTY_CLOSED])),
    ):
        r.check(f"foreign order in {where} => UNKNOWN", lookup(script).state, "UNKNOWN")


def t_missing_returned_client_id_is_unknown(r):
    no_id = {"status": "open"}
    r.check("missing in open", lookup(Script(OpenOrders=[{"open": {"X": no_id}}], ClosedOrders=[EMPTY_CLOSED])).state, "UNKNOWN")
    r.check("missing in closed", lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[{"closed": {"X": no_id}}])).state, "UNKNOWN")
    legacy_only = {"status": "open", "cl_" + "ordid": PID}
    r.check("legacy response field is NOT evidence",
            lookup(Script(OpenOrders=[{"open": {"X": legacy_only}}], ClosedOrders=[EMPTY_CLOSED])).state, "UNKNOWN")


def t_endpoint_errors_are_unknown(r):
    r.check("OpenOrders error", lookup(Script(OpenOrders=[kr.KrakenError(["EAPI:Rate limit exceeded"])])).state, "UNKNOWN")
    r.check("OpenOrders transport error", lookup(Script(OpenOrders=[TimeoutError("t")])).state, "UNKNOWN")
    r.check("ClosedOrders error", lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[kr.KrakenError(["EService:Busy"])])).state, "UNKNOWN")
    r.check("ClosedOrders transport error", lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[OSError("net")])).state, "UNKNOWN")


def t_malformed_payloads_are_unknown(r):
    bad_open = (None, [], "x", {}, {"open": None}, {"open": []}, {"open": {"O": "notadict"}}, {"open": {"": order()}})
    for payload in bad_open:
        r.check(f"open payload {payload!r}", lookup(Script(OpenOrders=[payload], ClosedOrders=[EMPTY_CLOSED])).state, "UNKNOWN")
    bad_closed = (None, [], {}, {"closed": None}, {"closed": []}, {"closed": {"C": 5}})
    for payload in bad_closed:
        r.check(f"closed payload {payload!r}", lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[payload])).state, "UNKNOWN")


def t_cursor_followed_to_exhaustion(r):
    script = Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[
        {"closed": {}, "cursor": {"next": "c1"}},
        {"closed": {}, "cursor": {"next": "c2"}},
        {"closed": {"C9": order(status="closed")}, "cursor": {"next": None}},
    ])
    res = lookup(script)
    r.check("found on the last page", (res.state, res.txid), ("FOUND", "C9"))
    closed = [p for e, p in script.calls if e == "ClosedOrders"]
    r.check("three pages fetched", len(closed), 3)
    r.check("first page sends no cursor", "cursor" in closed[0], False)
    r.check("page 2 sends cursor c1", closed[1].get("cursor"), "c1")
    r.check("page 3 sends cursor c2", closed[2].get("cursor"), "c2")
    r.check("count is not consulted", all("count" not in p for p in closed), True)


def t_count_is_not_completeness_proof(r):
    # A `count` that disagrees with the rows must not matter either way.
    res = lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[{"closed": {}, "count": 197}]))
    r.check("cursor exhaustion alone decides", res.state, "ABSENT")


def t_bad_cursors_are_unknown(r):
    cases = {
        "repeated cursor": [{"closed": {}, "cursor": {"next": "c1"}}, {"closed": {}, "cursor": {"next": "c1"}}],
        "cursor not an object": [{"closed": {}, "cursor": "c1"}],
        "next not a string": [{"closed": {}, "cursor": {"next": 7}}],
        "empty next": [{"closed": {}, "cursor": {"next": ""}}],
        "cursor list": [{"closed": {}, "cursor": ["c1"]}],
    }
    reasons = {
        "repeated cursor": "repeated", "cursor not an object": "cursor malformed",
        "next not a string": "cursor.next malformed", "empty next": "cursor.next malformed",
        "cursor list": "cursor malformed",
    }
    for name, pages in cases.items():
        res = lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=pages))
        r.check(name, res.state, "UNKNOWN")
        # The reason, not just the state: the page cap would also end in UNKNOWN.
        r.check(f"{name}: rejected for the right reason", reasons[name] in res.detail, True)
    endless = [{"closed": {}, "cursor": {"next": f"c{i}"}} for i in range(kr.LOOKUP_MAX_PAGES + 5)]
    res = lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=endless))
    r.check("incomplete pagination (page cap) => UNKNOWN", res.state, "UNKNOWN")
    r.check("the cap is the reason", "page cap" in res.detail, True)


def t_multiple_txids_are_unknown(r):
    res = lookup(Script(OpenOrders=[{"open": {"O1": order()}}], ClosedOrders=[{"closed": {"C1": order(status="closed")}}]))
    r.check("two distinct txids", res.state, "UNKNOWN")
    res = lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[{"closed": {"C1": order(), "C2": order()}}]))
    r.check("two distinct closed txids", res.state, "UNKNOWN")


def t_complete_zero_result_is_absent(r):
    res = lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[EMPTY_CLOSED]))
    r.check("state", res.state, "ABSENT")
    r.check("no txid", res.txid, None)
    multi = lookup(Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[
        {"closed": {}, "cursor": {"next": "a"}}, {"closed": {}}]))
    r.check("ABSENT only after the cursor is exhausted", multi.state, "ABSENT")


def t_invalid_inputs_make_no_kraken_call(r):
    for bad in (None, "", CL, CL + "-r1", "dca1-short", PID.upper()):
        script = Script()
        res = lookup(script, pid=bad)
        r.check(f"provider id {bad!r} => UNKNOWN", res.state, "UNKNOWN")
        r.check(f"provider id {bad!r} => no Kraken call", script.calls, [])
    for bad in (None, "", "garbage", "2026-10-06T12:00:00", 5):
        script = Script()
        r.check(f"started_at {bad!r} => UNKNOWN", lookup(script, started=bad).state, "UNKNOWN")
        r.check(f"started_at {bad!r} => no Kraken call", script.calls, [])


def t_request_shape_and_bounds(r):
    script = Script(OpenOrders=[EMPTY_OPEN], ClosedOrders=[EMPTY_CLOSED])
    lookup(script)
    endpoints = [e for e, _p in script.calls]
    r.check("OpenOrders is read BEFORE ClosedOrders", endpoints, ["OpenOrders", "ClosedOrders"])
    open_p, closed_p = script.calls[0][1], script.calls[1][1]
    r.check("open filter is the canonical field", open_p, {"cl_ord_id": PID})
    r.check("closed filter is the canonical field", closed_p["cl_ord_id"], PID)
    r.check("cursor mode requested", closed_p["with_cursor"], "true")
    r.check("closetime is explicit", closed_p["closetime"], "close")
    started = kr._lookup_started_at(STARTED)
    now = kr._lookup_started_at(NOW)
    r.check("start = execution_started_at minus the documented margin",
            closed_p["start"], str(int((started - kr.LOOKUP_START_MARGIN).timestamp())))
    r.check("end = now plus the forward skew margin",
            closed_p["end"], str(int((now + kr.LOOKUP_END_MARGIN).timestamp())))
    r.check("margin is 30 minutes", kr.LOOKUP_START_MARGIN, kr.timedelta(minutes=30))
    r.check("margin covers six cron cycles",
            kr.LOOKUP_START_MARGIN >= 6 * kr.timedelta(minutes=kr.CRON_CYCLE_MINUTES), True)
    r.check("margin exceeds the stale-claim threshold (15 min)",
            kr.LOOKUP_START_MARGIN > kr.timedelta(minutes=15), True)
    for _e, params in script.calls:
        r.check("no legacy field in lookup requests", ("cl_" + "ordid") in params, False)


def t_openorders_failure_stops_before_closed(r):
    script = Script(OpenOrders=[RuntimeError("down")])
    r.check("UNKNOWN", lookup(script).state, "UNKNOWN")
    r.check("ClosedOrders never read after an OpenOrders failure", [e for e, _ in script.calls], ["OpenOrders"])


TESTS = [
    ("known vectors", t_known_vectors),
    ("shape: 18 chars, prefix, charset", t_shape),
    ("distinctness and determinism", t_distinctness_and_determinism),
    ("NULL/unknown attempt_type derives nothing", t_null_and_unknown_attempt_type_derive_nothing),
    ("generation rules", t_generation_rules),
    ("missing internal id", t_missing_internal_id),
    ("validation and raw extraction", t_validation_and_raw_extraction),
    ("no legacy client-id field in source", t_no_legacy_client_id_field_in_source),
    ("lookup: open-only FOUND", t_open_only_is_found),
    ("lookup: closed-only FOUND", t_closed_only_is_found),
    ("lookup: same txid open+closed dedupes", t_same_txid_open_and_closed_dedupes),
    ("lookup: foreign order => UNKNOWN", t_foreign_order_from_ignored_filter_is_unknown),
    ("lookup: missing returned id => UNKNOWN", t_missing_returned_client_id_is_unknown),
    ("lookup: endpoint errors => UNKNOWN", t_endpoint_errors_are_unknown),
    ("lookup: malformed payloads => UNKNOWN", t_malformed_payloads_are_unknown),
    ("lookup: cursor followed to exhaustion", t_cursor_followed_to_exhaustion),
    ("lookup: count is not proof", t_count_is_not_completeness_proof),
    ("lookup: bad cursors => UNKNOWN", t_bad_cursors_are_unknown),
    ("lookup: multiple txids => UNKNOWN", t_multiple_txids_are_unknown),
    ("lookup: complete zero result => ABSENT", t_complete_zero_result_is_absent),
    ("lookup: invalid inputs make no call", t_invalid_inputs_make_no_kraken_call),
    ("lookup: request shape and bounds", t_request_shape_and_bounds),
    ("lookup: OpenOrders failure stops early", t_openorders_failure_stops_before_closed),
]

if __name__ == "__main__":
    raise SystemExit(Runner("ROB-21 provider identity and lookup").run(TESTS))
