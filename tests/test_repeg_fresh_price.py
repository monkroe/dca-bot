#!/usr/bin/env python3
"""Focused acceptance tests for ADR-0010 fresh-price revalidation.

Every test runs through _run_recovery_cycle, whose in-memory harness replaces
kraken_private, get_ticker_snapshot, get_asset_pair_info, sb_get, and sb_update.
No private or public Kraken endpoint can escape this test module.
"""
from _harness import kr, Runner
from test_repeg_decision import _recovery_row, _run_recovery_cycle


TERMINAL_ORIGINAL = {
    "status": "canceled",
    "vol_exec": "0.00000000",
    "cost": "0.00000",
    "fee": "0.00000",
    "price": "0.00000",
}


def _run(**kwargs):
    return _run_recovery_cycle(TERMINAL_ORIGINAL, **kwargs)


def _adds(trace):
    return [
        params for endpoint, params in trace["calls"]
        if endpoint == "AddOrder"
    ]


def _transition(trace):
    return trace["row"]["raw"]["repeg_transition"]


def _failure(trace):
    return _transition(trace).get("pre_submit_failure") or {}


def _pending_writes(trace):
    writes = []
    for _table, filters, updates in trace["updates"]:
        raw = kr._safe_json_load(updates.get("raw")) or {}
        transition = raw.get("repeg_transition") or {}
        if transition.get("phase") == "replacement_submission_pending":
            writes.append((filters, updates, transition))
    return writes


def _armed_row(price="0.03411"):
    row = _recovery_row("replacement_submission_pending")
    transition = row["raw"]["repeg_transition"]
    transition["replacement_request"] = {
        **transition["replacement_request"],
        "price": price,
        "volume": "291.23027",
    }
    transition["request_fingerprint"] = kr.hashlib.sha256(
        kr.json.dumps(
            transition["replacement_request"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    transition["replacement_submission_armed_at"] = (
        "2026-09-11T12:03:11+00:00"
    )
    return row


def t_fresh_bid_unchanged(r):
    trace = _run(ticker_snapshot={
        "bid": 0.03418, "ask": 0.03422, "mid": 0.03420,
    })
    r.check("unchanged fresh bid is submitted", _adds(trace)[0]["price"], "0.03418")
    r.check("one fresh ticker read occurs", trace["ticker_calls"], ["KASUSD"])


def t_fresh_bid_lower(r):
    trace = _run(ticker_snapshot={
        "bid": 0.03410, "ask": 0.03415, "mid": 0.034125,
    })
    r.check("lower fresh bid is submitted", _adds(trace)[0]["price"], "0.03410")


def t_fresh_bid_higher(r):
    trace = _run(ticker_snapshot={
        "bid": 0.03425, "ask": 0.03430, "mid": 0.034275,
    })
    r.check("higher fresh bid is submitted", _adds(trace)[0]["price"], "0.03425")


def t_fee_aware_volume_uses_fresh_price(r):
    fresh_bid = 0.03425
    trace = _run(ticker_snapshot={
        "bid": fresh_bid, "ask": 0.03430, "mid": 0.034275,
    })
    expected = kr.floor_to_decimals(
        (10.0 - kr.USD_SAFETY_MARGIN) / 1.004 / fresh_bid, 5)
    r.check(
        "fresh-price volume retains maker-fee allowance",
        _adds(trace)[0]["volume"],
        kr.format_volume(expected, 5),
    )


def t_stale_replacement_price_is_not_submitted(r):
    row = _recovery_row()
    stale = row["raw"]["repeg_transition"]["replacement_price"]
    trace = _run(
        row=row,
        ticker_snapshot={"bid": 0.03425, "ask": 0.03430, "mid": 0.034275},
    )
    r.check("historical replacement price is unchanged",
            _transition(trace)["replacement_price"], stale)
    r.check("AddOrder does not use historical price",
            _adds(trace)[0]["price"], "0.03425")


def t_ticker_read_failure_falls_back_without_add(r):
    trace = _run(ticker_error=RuntimeError("Ticker unavailable"))
    r.check("ticker failure reason is durable",
            _failure(trace).get("reason"), "fresh_market_read_failed")
    r.check("no snapshot is fabricated",
            "market_snapshot" in _failure(trace), False)
    r.check("ticker failure makes no AddOrder call", _adds(trace), [])
    r.check("ticker failure enters existing fallback", len(trace["fallback_calls"]), 1)


def t_unusable_fresh_book_falls_back_without_add(r):
    snapshot = {"bid": "invalid", "ask": 0.03422, "mid": 0.03420}
    trace = _run(ticker_snapshot=snapshot)
    r.check("unusable book has specific reason",
            _failure(trace).get("reason"), "fresh_market_unusable")
    r.check("unusable observation is retained",
            _failure(trace)["market_snapshot"]["bid"], "invalid")
    r.check("unusable book makes no AddOrder call", _adds(trace), [])


def t_crossed_book_preserves_observation_without_add(r):
    snapshot = {"bid": 0.03422, "ask": 0.03422, "mid": 0.03422}
    trace = _run(ticker_snapshot=snapshot)
    r.check("crossed book has specific reason",
            _failure(trace).get("reason"),
            "fresh_market_crossed_or_collapsed")
    r.check("crossed bid is retained",
            _failure(trace)["market_snapshot"]["bid"], snapshot["bid"])
    r.check("crossed ask is retained",
            _failure(trace)["market_snapshot"]["ask"], snapshot["ask"])
    r.check("crossed book makes no AddOrder call", _adds(trace), [])


def t_zero_replacement_volume(r):
    trace = _run(
        ticker_snapshot={"bid": 10.0, "ask": 11.0, "mid": 10.5},
        pair_info={"pair_decimals": 2, "lot_decimals": 0, "ordermin": 0.0},
        row={**_recovery_row(), "requested_quote_amount_base": 1.0},
    )
    r.check("zero volume has specific reason",
            _failure(trace).get("reason"), "replacement_volume_zero")
    r.check("zero volume makes no AddOrder call", _adds(trace), [])


def t_replacement_below_ordermin(r):
    trace = _run(pair_info={
        "pair_decimals": 5, "lot_decimals": 5, "ordermin": 500.0,
    })
    r.check("below ordermin has specific reason",
            _failure(trace).get("reason"), "replacement_below_ordermin")
    r.check("below ordermin makes no AddOrder call", _adds(trace), [])


def t_exhausted_budget_skips_external_io(r):
    row = {**_recovery_row(), "requested_quote_amount_base": 0.0}
    trace = _run(
        row=row,
        pair_info=AssertionError("AssetPairs must not be called"),
        ticker_error=AssertionError("Ticker must not be called"),
    )
    r.check("exhausted budget skips pair-info read",
            trace["pair_info_calls"], [])
    r.check("exhausted budget skips ticker read", trace["ticker_calls"], [])
    r.check("exhausted budget uses constraint failure",
            _failure(trace).get("reason"), "replacement_constraint_failed")
    r.check("exhausted budget makes no AddOrder call", _adds(trace), [])
    r.check("exhausted budget reaches fallback",
            len(trace["fallback_calls"]), 1)
    failure_writes = []
    for _table, _filters, updates in trace["updates"]:
        raw = kr._safe_json_load(updates.get("raw")) or {}
        transition = raw.get("repeg_transition") or {}
        if transition.get("pre_submit_failure"):
            failure_writes.append((updates, transition))
    r.check("exhausted failure uses one guarded fallback write",
            len(failure_writes), 1)
    updates, transition = failure_writes[0]
    r.check("exhausted failure write persists fallback status",
            updates.get("status"), "canceled_unfilled")
    r.check("exhausted failure write persists fallback phase",
            transition.get("phase"), "original_terminal_fallback")


def t_specific_failure_reasons_precede_generic(r):
    crossed = _run(ticker_snapshot={
        "bid": 0.03422, "ask": 0.03422, "mid": 0.03422,
    })
    generic = _run(pair_info=RuntimeError("AssetPairs unavailable"))
    r.check("known crossed condition is not generic",
            _failure(crossed).get("reason"),
            "fresh_market_crossed_or_collapsed")
    r.check("unclassified source constraint is generic",
            _failure(generic).get("reason"),
            "replacement_constraint_failed")


def t_submission_envelope_is_one_guarded_write_before_add(r):
    trace = _run(ticker_snapshot={
        "bid": 0.03419, "ask": 0.03423, "mid": 0.03421,
    })
    pending = _pending_writes(trace)
    r.check("one pending-envelope write exists", len(pending), 1)
    filters, updates, transition = pending[0]
    r.check("submission envelope write changes raw only", set(updates), {"raw"})
    r.check("submission write guards recovery status",
            filters.get("status"), f"eq.{kr.REPEG_RECOVERY_STATUS}")
    r.check_true("submission write guards original provider order",
                 filters.get("order_id", "").startswith("eq."))
    required = {
        "replacement_request", "submission_market_snapshot",
        "request_fingerprint", "replacement_cl_ord_id",
        "replacement_submission_armed_at", "phase",
    }
    r.check("complete envelope is present together",
            required.issubset(transition), True)
    db_raw_positions = [
        index for index, event in enumerate(trace["events"])
        if event == ("db", "raw")
    ]
    r.check_true("final envelope is durable before AddOrder",
                 db_raw_positions[-1]
                 < trace["events"].index(("kraken", "AddOrder")))


def t_failed_envelope_write_forbids_add(r):
    trace = _run(fail_submission_envelope=True)
    r.check("unconfirmed envelope write makes no AddOrder call", _adds(trace), [])
    r.check("failed envelope does not become durable",
            _transition(trace).get("phase"), "original_terminal")


def t_failure_evidence_and_fallback_are_one_write(r):
    trace = _run(ticker_error=RuntimeError("Ticker unavailable"))
    failure_writes = []
    for _table, _filters, updates in trace["updates"]:
        raw = kr._safe_json_load(updates.get("raw")) or {}
        transition = raw.get("repeg_transition") or {}
        if transition.get("pre_submit_failure"):
            failure_writes.append((updates, transition))
    r.check("one write contains failure evidence", len(failure_writes), 1)
    updates, transition = failure_writes[0]
    r.check("same write persists fallback status",
            updates.get("status"), "canceled_unfilled")
    r.check("same write persists fallback phase",
            transition.get("phase"), "original_terminal_fallback")
    r.check("no standalone observability write",
            set(updates) == {"raw"}, False)


def t_direct_attachment_uses_submission_telemetry(r):
    fresh = {"bid": 0.03419, "ask": 0.03423, "mid": 0.03421}
    trace = _run(ticker_snapshot=fresh)
    submission = _transition(trace)["submission_market_snapshot"]
    r.check("direct attachment bid is submit-time", trace["row"]["bid"], fresh["bid"])
    r.check("direct attachment ask is submit-time", trace["row"]["ask"], fresh["ask"])
    r.check("direct attachment mid is submit-time", trace["row"]["mid"], fresh["mid"])
    r.check("direct attachment timestamp is submit-time",
            trace["row"]["mid_ts"], submission["observed_at"])


def t_ambiguous_attachment_uses_submission_telemetry(r):
    row = _armed_row("0.03425")
    transition = row["raw"]["repeg_transition"]
    transition["submission_market_snapshot"] = {
        "bid": 0.03425, "ask": 0.03430, "mid": 0.034275,
        "observed_at": "2026-09-11T12:03:10+00:00",
    }
    provider_cl = transition["replacement_cl_ord_id"]
    trace = _run_recovery_cycle(
        row=row,
        replacement_open={"O-FOUND": {
            "status": "open", "vol_exec": "0.00000000",
            "cl_ordid": provider_cl,
        }},
        ticker_snapshot={"bid": 0.05000, "ask": 0.06000, "mid": 0.05500},
    )
    r.check("ambiguous attach uses submit bid", trace["row"]["bid"], 0.03425)
    r.check("ambiguous attach uses submit timestamp",
            trace["row"]["mid_ts"], "2026-09-11T12:03:10+00:00")
    r.check("ambiguous attach does not re-read ticker", trace["ticker_calls"], [])


def t_legacy_attachment_uses_decision_telemetry(r):
    row = _armed_row()
    transition = row["raw"]["repeg_transition"]
    transition.pop("submission_market_snapshot", None)
    provider_cl = transition["replacement_cl_ord_id"]
    trace = _run_recovery_cycle(
        row=row,
        replacement_open={"O-LEGACY": {
            "status": "open", "vol_exec": "0.00000000",
            "cl_ordid": provider_cl,
        }},
    )
    decision = transition["market_snapshot"]
    r.check("legacy attach falls back to decision bid",
            trace["row"]["bid"], decision["bid"])
    r.check("legacy attach falls back to decision timestamp",
            trace["row"]["mid_ts"], decision["observed_at"])


def t_attachment_limit_price_comes_from_exact_request(r):
    direct = _run(ticker_snapshot={
        "bid": 0.03425, "ask": 0.03430, "mid": 0.034275,
    })
    row = _armed_row("0.03427")
    provider_cl = row["raw"]["repeg_transition"]["replacement_cl_ord_id"]
    ambiguous = _run_recovery_cycle(
        row=row,
        replacement_open={"O-LIMIT": {
            "status": "open", "vol_exec": "0.00000000",
            "cl_ordid": provider_cl,
        }},
    )
    r.check("direct limit price matches exact request",
            direct["row"]["limit_price"], 0.03425)
    r.check("ambiguous limit price matches exact request",
            ambiguous["row"]["limit_price"], 0.03427)


def t_repeg_count_is_unchanged(r):
    trace = _run(ticker_snapshot={
        "bid": 0.03425, "ask": 0.03430, "mid": 0.034275,
    })
    r.check("fresh submission does not increment generation count",
            trace["row"]["raw"]["repeg_count"], 1)
    r.check("fresh submission stays generation one",
            _transition(trace)["generation"], 1)


def t_deterministic_client_identity_is_unchanged(r):
    row = _recovery_row()
    original_identity = row["raw"]["repeg_transition"]["replacement_cl_ord_id"]
    trace = _run(row=row, ticker_snapshot={
        "bid": 0.03425, "ask": 0.03430, "mid": 0.034275,
    })
    r.check("transition client identity is unchanged",
            _transition(trace)["replacement_cl_ord_id"], original_identity)
    r.check("submitted client identity is unchanged",
            _adds(trace)[0]["cl_ordid"], original_identity)


def t_ambiguous_response_is_not_blindly_resubmitted(r):
    first = _run(add_order_error=TimeoutError("response lost"))
    exact_request = dict(_transition(first)["replacement_request"])
    second = _run_recovery_cycle(
        row=first["row"],
        ticker_snapshot={"bid": 0.05000, "ask": 0.06000, "mid": 0.05500},
    )
    r.check("ambiguous submission happened at most once", len(_adds(first)), 1)
    r.check("later recovery makes no AddOrder call", _adds(second), [])
    r.check("later recovery makes no fresh ticker read", second["ticker_calls"], [])
    r.check("later recovery preserves exact request",
            _transition(second)["replacement_request"], exact_request)


def t_cumulative_budget_invariant_uses_fresh_price(r):
    observation = {
        "status": "canceled", "vol_exec": "58.50000000",
        "cost": "2.00000", "fee": "0.00800", "price": "0.03418",
    }
    trace = _run_recovery_cycle(
        observation,
        ticker_snapshot={"bid": 0.03430, "ask": 0.03435, "mid": 0.034325},
    )
    request = _adds(trace)[0]
    reserved = float(request["volume"]) * float(request["price"]) * 1.004
    remaining = 10.0 - 2.0 - 0.008
    r.check_true("fresh request stays inside cumulative remaining budget",
                 reserved <= remaining)
    r.check("prior cost remains durable", trace["row"]["filled_quote_cost"], 2.0)
    r.check("prior fee remains durable", trace["row"]["fee_quote"], 0.008)


def t_post_arm_recovery_cannot_reprice_request(r):
    row = _armed_row("0.03411")
    exact_request = kr.json.loads(kr.json.dumps(
        row["raw"]["repeg_transition"]["replacement_request"]))
    provider_cl = row["raw"]["repeg_transition"]["replacement_cl_ord_id"]
    trace = _run_recovery_cycle(
        row=row,
        replacement_open={"O-POST-ARM": {
            "status": "open", "vol_exec": "0.00000000",
            "cl_ordid": provider_cl,
        }},
        ticker_snapshot={"bid": 0.05000, "ask": 0.06000, "mid": 0.05500},
    )
    r.check("post-arm recovery performs no fresh ticker read",
            trace["ticker_calls"], [])
    r.check("post-arm recovery performs no AddOrder",
            _adds(trace), [])
    r.check("post-arm request is immutable",
            _transition(trace)["replacement_request"], exact_request)
    r.check("post-arm recovery reconciles provider order",
            trace["row"]["order_id"], "O-POST-ARM")


def t_pre_arm_recovery_constructs_fresh_request(r):
    row = _recovery_row("original_terminal")
    decision_request = dict(
        row["raw"]["repeg_transition"]["replacement_request"])
    trace = _run(
        row=row,
        ticker_snapshot={"bid": 0.03425, "ask": 0.03430, "mid": 0.034275},
    )
    r.check("pre-arm recovery obtains fresh market data",
            trace["ticker_calls"], ["KASUSD"])
    r.check("pre-arm request is reconstructed at fresh bid",
            _adds(trace)[0]["price"], "0.03425")
    r.check_true("pre-arm exact request may replace decision candidate",
                 _adds(trace)[0] != decision_request)


TESTS = [
    ("fresh bid unchanged", t_fresh_bid_unchanged),
    ("fresh bid lower", t_fresh_bid_lower),
    ("fresh bid higher", t_fresh_bid_higher),
    ("fee-aware fresh-price volume", t_fee_aware_volume_uses_fresh_price),
    ("stale price excluded from AddOrder", t_stale_replacement_price_is_not_submitted),
    ("ticker failure safe fallback", t_ticker_read_failure_falls_back_without_add),
    ("unusable book safe fallback", t_unusable_fresh_book_falls_back_without_add),
    ("crossed book safe fallback", t_crossed_book_preserves_observation_without_add),
    ("zero replacement volume", t_zero_replacement_volume),
    ("below ordermin", t_replacement_below_ordermin),
    ("exhausted budget skips external I/O", t_exhausted_budget_skips_external_io),
    ("specific failure reasons", t_specific_failure_reasons_precede_generic),
    ("one guarded envelope write", t_submission_envelope_is_one_guarded_write_before_add),
    ("failed envelope write", t_failed_envelope_write_forbids_add),
    ("failure evidence atomic with fallback", t_failure_evidence_and_fallback_are_one_write),
    ("direct submit-time telemetry", t_direct_attachment_uses_submission_telemetry),
    ("ambiguous submit-time telemetry", t_ambiguous_attachment_uses_submission_telemetry),
    ("legacy decision-time telemetry", t_legacy_attachment_uses_decision_telemetry),
    ("attachment exact-request limit", t_attachment_limit_price_comes_from_exact_request),
    ("repeg count unchanged", t_repeg_count_is_unchanged),
    ("deterministic client identity unchanged", t_deterministic_client_identity_is_unchanged),
    ("ambiguous response no blind retry", t_ambiguous_response_is_not_blindly_resubmitted),
    ("cumulative budget invariant", t_cumulative_budget_invariant_uses_fresh_price),
    ("post-arm request immutable", t_post_arm_recovery_cannot_reprice_request),
    ("pre-arm request reconstructed", t_pre_arm_recovery_constructs_fresh_request),
]


if __name__ == "__main__":
    raise SystemExit(Runner("ADR-0010 fresh-price revalidation").run(TESTS))
