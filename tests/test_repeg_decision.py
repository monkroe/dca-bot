#!/usr/bin/env python3
"""Branch coverage for _repeg_decision().

This is the only evidence that the bid-chase logic is correct. Re-peg has never
fired against Kraken (as of 2026-07-28), and the Phase 2 acceptance verdict has
a deadline of 2026-08-11 after which it closes with re-peg marked
`untested-in-production`. Until a live fill exists, THIS FILE is the proof --
which is why it belongs in the repository rather than in a chat transcript.

Guard order matters and is asserted directly: repeg_max is checked before
everything else, so an exhausted counter must skip even when every other
condition would fire. A reordering that "looks equivalent" would let a leg
re-peg past its limit.

KAS numbers throughout: tick 0.00001, lot_decimals 8.
"""
from _harness import kr, Runner

RP = kr._repeg_decision

TICK = 0.00001
LOT = 8
ORDERMIN = 100.0        # KAS ordermin, well below a real leg
COST = 5.00             # quote budget for the leg
BASE = 0.02800          # our resting limit


def call(**over):
    """Defaults describe a healthy leg that SHOULD re-peg; each test perturbs one thing."""
    kwargs = dict(
        cur_price=BASE,
        bid=BASE + TICK,
        ask=BASE + 5 * TICK,
        ref_price=0.02500,
        h90=None,
        cap_pct=0.20,
        require_above_h90=False,
        tick=TICK,
        min_ticks=1,
        repeg_count=0,
        repeg_max=3,
        ordermin=ORDERMIN,
        cost_target=COST,
        lot_decimals=LOT,
    )
    kwargs.update(over)
    return RP(**kwargs)


def t_happy_path(r):
    action, detail = call()
    r.check("healthy leg re-pegs", action, "repeg")
    r.check("new price is the best bid", detail, BASE + TICK)


def t_max_reached(r):
    r.check("count == max -> skip", call(repeg_count=3)[1], "repeg_max reached")
    r.check("count > max -> skip", call(repeg_count=4)[1], "repeg_max reached")


def t_max_minus_one_allowed(r):
    r.check("count == max-1 still fires", call(repeg_count=2)[0], "repeg")


def t_max_checked_first(r):
    # Everything else is also wrong here; the reason must still be the counter.
    action, detail = call(repeg_count=3, bid=BASE - TICK, ask=BASE - TICK)
    r.check("counter is the FIRST guard", detail, "repeg_max reached")
    r.check("and it skips", action, "skip")


def t_bid_not_above(r):
    r.check("bid equal to our price -> skip", call(bid=BASE)[1], "bid not above resting price")
    r.check("bid below our price -> skip", call(bid=BASE - TICK)[1], "bid not above resting price")


def t_bid_exactly_at_threshold(r):
    # Guard is `bid < cur_price + min_ticks * tick`, so the threshold itself fires.
    r.check("bid exactly 1 tick above -> repeg", call(bid=BASE + TICK)[0], "repeg")


def t_min_ticks_two(r):
    # min_ticks is CONFIGURATION (repeg_min_ticks), not a constant. Raising it
    # raises the bar, and the near-miss must skip.
    r.check("min_ticks 2, bid +1 tick -> skip",
            call(min_ticks=2, bid=BASE + TICK)[1], "bid not above resting price")
    r.check("min_ticks 2, bid +2 ticks -> repeg",
            call(min_ticks=2, bid=BASE + 2 * TICK)[0], "repeg")


def t_spread_collapsed(r):
    r.check("bid above ask -> skip",
            call(bid=BASE + 5 * TICK, ask=BASE + 2 * TICK)[1], "spread collapsed (would cross)")


def t_bid_equals_ask(r):
    # `bid >= ask` -- posting at the ask would cross and lose maker status.
    px = BASE + 3 * TICK
    r.check("bid == ask -> skip", call(bid=px, ask=px)[1], "spread collapsed (would cross)")


def t_cap_vetoes(r):
    # The new bid is above ref * 1.20 -> the same veto the taker fallback respects.
    action, detail = call(bid=0.03100, ask=0.03200, ref_price=0.02500, cap_pct=0.20)
    r.check("above cap -> skip", action, "skip")
    r.check_true("reason names the cap", detail.startswith("above cap ("))


def t_cap_boundary_allows(r):
    # Exactly at the cap is NOT above it, so the leg still re-pegs.
    ref = 0.02500
    r.check("bid exactly at cap -> repeg",
            call(bid=ref * 1.20, ask=ref * 1.30, ref_price=ref, cap_pct=0.20)[0], "repeg")


def t_cap_missing_reference(r):
    # DP-4 again: no reference means no veto, here as well.
    r.check("ref None -> re-peg proceeds", call(ref_price=None, bid=0.05000, ask=0.06000)[0], "repeg")


def t_cap_h90_guard_reaches_repeg(r):
    # The H90 floor must apply on this path too, not only at T0.
    r.check("above cap but below H90 -> repeg",
            call(bid=0.03100, ask=0.03200, ref_price=0.02500,
                 h90=0.04000, require_above_h90=True)[0], "repeg")
    r.check("above cap and above H90 -> skip",
            call(bid=0.03100, ask=0.03200, ref_price=0.02500,
                 h90=0.03000, require_above_h90=True)[0], "skip")


def t_below_ordermin(r):
    # A budget too small for the exchange minimum must not produce a doomed order.
    r.check("volume under ordermin -> skip",
            call(ordermin=1_000_000.0)[1], "new_vol below ordermin")


def t_volume_zero(r):
    # lot_decimals 0 truncates 5.00 / 0.028 = 178.5 -> 178, still fine; make the
    # budget tiny so the floor lands on zero.
    r.check("volume floors to 0 -> skip",
            call(cost_target=0.001, lot_decimals=0, ordermin=0.0)[1], "new_vol below ordermin")


def t_volume_exactly_ordermin(r):
    # `new_vol < ordermin` is the guard, so equality proceeds.
    bid = BASE + TICK
    vol = kr.floor_to_decimals(COST / bid, LOT)
    r.check("volume == ordermin -> repeg", call(ordermin=vol)[0], "repeg")


def t_returns_bid_not_ask(r):
    # The re-post must be at the BID. Posting at the ask would cross and the
    # post-only flag would reject it, dropping the day to a taker fallback.
    _, detail = call(bid=BASE + 2 * TICK, ask=BASE + 9 * TICK)
    r.check("re-post price is the bid", detail, BASE + 2 * TICK)


def _run_cancel_open_readback_race(arm_succeeds=True, *, source_row=None,
                                   cancel_readback=None):
    """Run the confirmed race and return its observable call/state trace."""
    oid = "O756Z5-SM7WB-5IAX7D"
    cl = "dca-KASUSD-2026-09-11-704"
    calls = []
    state_updates = []
    fallback_calls = []
    probes = []
    query_readbacks = []
    sleep_calls = []
    trace_events = []
    immediate_readback = cancel_readback or {
        "status": "open", "vol_exec": "0.00000000"}
    later_readback = {"status": "canceled", "vol_exec": "0.00000000"}

    def fake_kraken(endpoint, params=None):
        trace_events.append(("kraken", endpoint))
        calls.append((endpoint, dict(params or {})))
        if endpoint == "CancelOrder":
            return {"count": 1}
        if endpoint == "QueryOrders":
            response = immediate_readback if not query_readbacks else later_readback
            query_readbacks.append(dict(response))
            return {oid: response}
        if endpoint == "AddOrder":
            return {"txid": ["OTEST-REPEG-REPLACEMENT"]}
        raise AssertionError(f"unexpected Kraken endpoint: {endpoint}")

    def fake_probe(_row, action, detail, **context):
        probes.append((action, detail, context))

    def fake_update(table, filters, updates):
        trace_events.append(("db", updates.get("status")))
        state_updates.append((table, filters, updates))
        if updates.get("status") == getattr(kr, "REPEG_RECOVERY_STATUS", None):
            if not arm_succeeds:
                return []
        return [{**updates, "cl_ord_id": cl, "order_id": oid}]

    def fake_fallback(*args, **kwargs):
        fallback_calls.append((args, kwargs))

    replacements = {
        "kraken_private": fake_kraken,
        "get_asset_pair_info": lambda _pair: {
            "pair_decimals": 5, "lot_decimals": 5, "ordermin": 200.0,
        },
        "get_ticker_snapshot": lambda _pair: {
            "bid": 0.03418, "ask": 0.03422, "mid": 0.03420,
        },
        "get_cap_context": lambda _pair, _user, _settings: (
            0.034406, 0.028812, "H7",
        ),
        "log_repeg_probe": fake_probe,
        "sb_update": fake_update,
        "_fallback_decision": fake_fallback,
    }
    originals = {name: getattr(kr, name) for name in replacements}
    real_sleep = kr.time.sleep
    for name, replacement in replacements.items():
        setattr(kr, name, replacement)
    kr.time.sleep = lambda seconds: sleep_calls.append(seconds)

    source = source_row or {
        "cl_ord_id": cl,
        "pair": "KASUSD",
        "order_id": oid,
        "trade_date_chicago": "2026-09-11",
        "parent_event_id": "0ef48173-95a2-45ba-a406-38d7652a32c6",
        "dca_order_id": 1,
        "execution_started_at": "2026-09-11T11:53:15+00:00",
        "limit_price": 0.03413,
        "requested_quote_amount_base": 10.0,
        "raw": '{"repeg_count": 0}',
    }
    oid = source["order_id"]
    cl = source["cl_ord_id"]

    try:
        window_end = kr.datetime(2026, 9, 11, 7, 9, tzinfo=kr.CHICAGO_TZ)
        handled = kr._maybe_repeg(
            source,
            {"status": "open", "vol_exec": "0.00000000"},
            {
                "repeg_enabled": True,
                "repeg_max": 5,
                "repeg_min_ticks": 1,
                "maker_fee_rate": 0.004,
                "cap_pct": 0.20,
                "cap_require_above_h90": True,
            },
            "test-user",
            window_end,
        )
    finally:
        kr.time.sleep = real_sleep
        for name, original in originals.items():
            setattr(kr, name, original)

    return {
        "oid": oid,
        "cl": cl,
        "calls": calls,
        "state_updates": state_updates,
        "fallback_calls": fallback_calls,
        "probes": probes,
        "query_readbacks": query_readbacks,
        "sleep_calls": sleep_calls,
        "trace_events": trace_events,
        "handled": handled,
    }


def t_2026_09_11_cancel_open_readback_incident_characterization(r):
    """Historical characterization of the defective 2026-09-11 behavior.

    This is evidence, not the long-term contract. It is expected to fail once
    the production fix intentionally changes this path. At that point delete
    this case or explicitly re-date it as history; never "repair" its expected
    values to match the new behavior.
    """
    trace = _run_cancel_open_readback_race()
    calls = trace["calls"]
    endpoints = [endpoint for endpoint, _params in calls]
    add_order_calls = [params for endpoint, params in calls if endpoint == "AddOrder"]
    immediate_readback = trace["query_readbacks"][0]
    persisted = repr(trace["state_updates"])
    submitted = repr(add_order_calls)

    r.check("re-peg decision selected", trace["probes"][0][0], "repeg")
    r.check("cancel immediately followed by readback",
            endpoints[:2], ["CancelOrder", "QueryOrders"])
    r.check("cancel targets original maker", calls[0][1]["txid"], trace["oid"])
    r.check("immediate readback is nonterminal", immediate_readback["status"], "open")
    r.check("immediate readback reports no fill", float(immediate_readback["vol_exec"]), 0.0)
    r.check("current path exits as unhandled", trace["handled"], False)
    r.check("replacement AddOrder is not reached", add_order_calls, [])
    r.check("no -r1 replacement is submitted", "-r1" in submitted, False)
    r.check("no -r1 replacement is persisted", "-r1" in persisted, False)
    r.check("fallback is not submitted in the immediate branch",
            trace["fallback_calls"], [])


def t_cancel_open_readback_requires_observable_continuation(r):
    """Behavioral gate: the accepted-cancel transition must remain observable."""
    trace = _run_cancel_open_readback_race()
    cl = trace["cl"]
    add_order_calls = [
        params for endpoint, params in trace["calls"] if endpoint == "AddOrder"
    ]

    def targets_original_execution(update_call):
        table, filters, _updates = update_call
        return (table == "dca_executions"
                and filters.get("cl_ord_id") == f"eq.{cl}")

    def persisted_replacement_identity(update_call):
        if not targets_original_execution(update_call):
            return False
        raw = kr._safe_json_load(update_call[2].get("raw")) or {}
        next_cl = raw.get("kraken_cl")
        return isinstance(next_cl, str) and next_cl.startswith(f"{cl}-r")

    def explicitly_persists_recovery(update_call):
        if not targets_original_execution(update_call):
            return False
        return update_call[2].get("status") == kr.REPEG_RECOVERY_STATUS

    # Observable contract only. It allows a later readback and replacement,
    # persisted replacement identity, or an explicitly durable recovery state.
    # Generic writes, raw snapshots, telemetry, timestamps, and unrelated rows
    # do not satisfy either branch.
    replacement_initiated = (
        any(str(params.get("cl_ordid") or "").startswith(f"{cl}-r")
            for params in add_order_calls)
        or any(persisted_replacement_identity(call)
               for call in trace["state_updates"])
    )
    recovery_persisted = any(
        explicitly_persists_recovery(call) for call in trace["state_updates"]
    )
    r.check_true(
        "cancel/open race has observable replacement or recovery continuation",
        replacement_initiated or recovery_persisted,
    )


def t_repeg_recovery_is_armed_before_cancel(r):
    trace = _run_cancel_open_readback_race()
    cl = trace["cl"]
    oid = trace["oid"]
    recovery_writes = [
        call for call in trace["state_updates"]
        if call[0] == "dca_executions"
        and call[2].get("status") == kr.REPEG_RECOVERY_STATUS
    ]

    r.check("one durable recovery entry", len(recovery_writes), 1)
    table, filters, updates = recovery_writes[0]
    r.check("entry targets executions", table, "dca_executions")
    r.check("entry CAS targets original client id",
            filters.get("cl_ord_id"), f"eq.{cl}")
    r.check("entry CAS requires limit_open",
            filters.get("status"), "eq.limit_open")
    r.check("entry CAS requires original provider order",
            filters.get("order_id"), f"eq.{oid}")
    r.check("durable entry precedes cancel",
            trace["trace_events"][:2],
            [("db", kr.REPEG_RECOVERY_STATUS), ("kraken", "CancelOrder")])

    raw = kr._safe_json_load(updates.get("raw")) or {}
    transition = raw.get("repeg_transition") or {}
    r.check("replacement identity is deterministic",
            transition.get("replacement_cl_ord_id"), f"{cl}-r1")
    r.check("original provider identity is retained",
            transition.get("original_order_id"), oid)
    r.check("original maker price is retained",
            transition.get("original_limit_price"), 0.03413)
    r.check_true("maker deadline is frozen", transition.get("maker_deadline"))
    r.check_true("window end is frozen", transition.get("window_end"))
    r.check_true("fallback cutoff is frozen", transition.get("fallback_cutoff"))
    r.check_true("manual cutoff is frozen", transition.get("manual_at"))


def t_repeg_cancel_requires_successful_durable_claim(r):
    trace = _run_cancel_open_readback_race(arm_succeeds=False)
    r.check("ambiguous CAS leaves original maker untouched", trace["calls"], [])
    r.check("failed ownership claim is not handled", trace["handled"], False)


def _recovery_row(phase="cancel_requested"):
    cl = "dca-KASUSD-2026-09-11-704"
    oid = "O756Z5-SM7WB-5IAX7D"
    window_end = kr.datetime(2026, 9, 11, 7, 9, tzinfo=kr.CHICAGO_TZ)
    transition = {
        "phase": phase,
        "generation": 1,
        "original_order_id": oid,
        "original_cl_ord_id": cl,
        "original_provider_cl_ord_id": cl,
        "replacement_cl_ord_id": f"{cl}-r1",
        "replacement_price": 0.03418,
        "generation_fills": [],
        "market_snapshot": {
            "bid": 0.03418, "ask": 0.03422, "mid": 0.03420,
            "observed_at": "2026-09-11T11:58:10+00:00",
        },
        "replacement_request": {
            "pair": "KASUSD", "type": "buy", "ordertype": "limit",
            "price": "0.03418", "volume": "291.23027",
            "oflags": "post,fciq", "cl_ordid": f"{cl}-r1",
        },
        "request_fingerprint": "test-fingerprint",
        "transition_at": "2026-09-11T11:58:10+00:00",
        "maker_deadline": (
            window_end - kr.timedelta(minutes=kr.CRON_CYCLE_MINUTES)
        ).isoformat(),
        "window_end": window_end.isoformat(),
        "fallback_cutoff": (
            window_end + kr.timedelta(minutes=kr.I6_GRACE_MINUTES)
        ).isoformat(),
        "manual_at": (
            window_end + kr.timedelta(minutes=kr.LIMIT_TTL_MINUTES)
        ).isoformat(),
    }
    return {
        "cl_ord_id": cl,
        "order_id": oid,
        "pair": "KASUSD",
        "status": kr.REPEG_RECOVERY_STATUS,
        "trade_date_chicago": "2026-09-11",
        "requested_quote_amount_base": 10.0,
        "parent_event_id": "0ef48173-95a2-45ba-a406-38d7652a32c6",
        "dca_order_id": 1,
        "raw": {"kraken_cl": f"{cl}-r1", "repeg_count": 1,
                "repeg_transition": transition},
        "execution_started_at": "2026-09-11T11:53:15+00:00",
        "limit_price": 0.03413,
        "mid": 0.03420,
        "filled_quote_cost": 0.0,
        "fee_quote": 0.0,
    }


_DEFAULT_TICKER = object()


def _run_recovery_cycle(observation=None, *, row=None, now=None,
                        query_error=None, add_order_error=None,
                        replacement_open=None, replacement_closed=None,
                        include_transitioned_open=False,
                        advance_before_manual=False,
                        ticker_snapshot=_DEFAULT_TICKER, ticker_error=None,
                        pair_info=None, fail_submission_envelope=False):
    """Run the run_maker_inspection recovery owner against an in-memory row."""
    db_row = dict(row or _recovery_row())
    db_row["raw"] = kr._safe_json_load(db_row.get("raw")) or {}
    calls = []
    updates = []
    selector_filters = []
    fallback_calls = []
    finalize_calls = []
    events = []
    ticker_calls = []
    pair_info_calls = []

    def matches(filters):
        for key in ("cl_ord_id", "status", "order_id"):
            value = filters.get(key)
            if isinstance(value, str) and value.startswith("eq."):
                if str(db_row.get(key)) != value[3:]:
                    return False
        raw_filter = filters.get("raw")
        if isinstance(raw_filter, str) and raw_filter.startswith("eq."):
            expected = kr._safe_json_load(raw_filter[3:])
            if expected != (kr._safe_json_load(db_row.get("raw")) or {}):
                return False
        return True

    def fake_get(table, params=None):
        params = dict(params or {})
        if table != "dca_executions":
            return []
        status = params.get("status")
        if status == f"eq.{kr.REPEG_RECOVERY_STATUS}":
            selector_filters.append(params)
            return [dict(db_row)] if db_row.get("status") == kr.REPEG_RECOVERY_STATUS else []
        if status == "eq.limit_open":
            if include_transitioned_open and db_row.get("status") == "limit_open":
                return [dict(db_row)]
            return []
        if isinstance(status, str) and status.startswith("in.("):
            return []
        if params.get("cl_ord_id") == f"eq.{db_row['cl_ord_id']}":
            return [dict(db_row)]
        return []

    def fake_update(table, filters, changed):
        updates.append((table, dict(filters), dict(changed)))
        events.append(("db", changed.get("status") or "raw"))
        if advance_before_manual and changed.get("status") == "manual_required":
            db_row.update({"status": "limit_open", "order_id": "O-ADVANCED"})
            advanced_raw = kr._safe_json_load(db_row.get("raw")) or {}
            advanced_raw["repeg_transition"]["phase"] = "replacement_attached"
            db_row["raw"] = advanced_raw
        changed_raw = kr._safe_json_load(changed.get("raw")) or {}
        changed_transition = changed_raw.get("repeg_transition") or {}
        if (fail_submission_envelope
                and changed_transition.get("phase")
                == "replacement_submission_pending"
                and set(changed) == {"raw"}):
            return []
        if table != "dca_executions" or not matches(filters):
            return []
        db_row.update(changed)
        if "raw" in changed:
            db_row["raw"] = kr._safe_json_load(changed["raw"]) or {}
        return [dict(db_row)]

    def fake_kraken(endpoint, params=None):
        params = dict(params or {})
        calls.append((endpoint, params))
        events.append(("kraken", endpoint))
        if endpoint == "QueryOrders":
            if query_error:
                raise query_error
            oid = params.get("txid")
            if oid == db_row["order_id"]:
                return {} if observation is None else {oid: observation}
            return {}
        if endpoint == "CancelOrder":
            return {"count": 1}
        if endpoint == "AddOrder":
            if add_order_error:
                raise add_order_error
            return {"txid": ["OTEST-REPEG-REPLACEMENT"]}
        if endpoint == "OpenOrders":
            return {"open": replacement_open or {}}
        if endpoint == "ClosedOrders":
            return {"closed": replacement_closed or {}}
        raise AssertionError(f"unexpected Kraken endpoint: {endpoint}")

    def fake_ticker(pair):
        ticker_calls.append(pair)
        if ticker_error:
            raise ticker_error
        if ticker_snapshot is _DEFAULT_TICKER:
            return {"bid": 0.03418, "ask": 0.03422, "mid": 0.03420}
        return ticker_snapshot

    def fake_pair_info(pair):
        pair_info_calls.append(pair)
        if isinstance(pair_info, Exception):
            raise pair_info
        return pair_info or {
            "pair_decimals": 5, "lot_decimals": 5, "ordermin": 200.0,
        }

    replacements = {
        "sb_get": fake_get,
        "sb_update": fake_update,
        "kraken_private": fake_kraken,
        "save_mid_snapshot": lambda *_args, **_kwargs: None,
        "get_ticker_snapshot": fake_ticker,
        "_fallback_decision": lambda *args, **kwargs: fallback_calls.append((args, kwargs)),
        "finalize_order": lambda *args, **kwargs: finalize_calls.append((args, kwargs)),
        "get_asset_pair_info": fake_pair_info,
        "tg_send": lambda *_args, **_kwargs: None,
    }
    originals = {name: getattr(kr, name) for name in replacements}
    for name, replacement in replacements.items():
        setattr(kr, name, replacement)
    try:
        kr.run_maker_inspection(
            {"maker_fee_rate": 0.004, "taker_fee_rate": 0.008,
             "time_window_minutes": 30},
            "test-user",
            now or kr.datetime(2026, 9, 11, 6, 59, tzinfo=kr.CHICAGO_TZ),
        )
    finally:
        for name, original in originals.items():
            setattr(kr, name, original)
    return {
        "row": db_row, "calls": calls, "updates": updates,
        "selector_filters": selector_filters, "fallback_calls": fallback_calls,
        "finalize_calls": finalize_calls, "events": events,
        "ticker_calls": ticker_calls, "pair_info_calls": pair_info_calls,
    }


def t_recovery_writer_and_owner_share_status_constant(r):
    entry = _run_cancel_open_readback_race()
    armed = next(call for call in entry["state_updates"]
                 if call[2].get("status") == kr.REPEG_RECOVERY_STATUS)
    row = _recovery_row()
    row.update(armed[2])
    trace = _run_recovery_cycle(
        {"status": "open", "vol_exec": "0.00000000"}, row=row)

    r.check("writer persists centralized status",
            armed[2]["status"], kr.REPEG_RECOVERY_STATUS)
    r.check("owner selects centralized status",
            trace["selector_filters"][0]["status"],
            f"eq.{kr.REPEG_RECOVERY_STATUS}")
    r.check_true("writer row is visible to recovery owner",
                 any(endpoint == "QueryOrders" for endpoint, _ in trace["calls"]))


def t_recovery_open_zero_fill_never_spends(r):
    trace = _run_recovery_cycle({"status": "open", "vol_exec": "0.00000000"})
    spend_calls = [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"]
    r.check("open original remains recovery pending",
            trace["row"]["status"], kr.REPEG_RECOVERY_STATUS)
    r.check("open original has no replacement", spend_calls, [])
    r.check("open original has no fallback", trace["fallback_calls"], [])


def t_recovery_terminal_zero_fill_replaces_before_deadline(r):
    for status in ("canceled", "expired"):
        trace = _run_recovery_cycle({
            "status": status, "vol_exec": "0.00000000",
            "cost": "0.00000", "fee": "0.00000", "price": "0.00000",
        })
        adds = [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"]
        r.check(f"{status} starts one replacement", len(adds), 1)
        r.check(f"{status} replacement uses persisted identity",
                adds[0].get("cl_ordid"),
                _recovery_row()["raw"]["repeg_transition"]["replacement_cl_ord_id"])
        r.check(f"{status} replacement becomes owned limit",
                trace["row"]["status"], "limit_open")


def t_recovery_terminal_at_deadline_uses_existing_fallback(r):
    trace = _run_recovery_cycle(
        {"status": "canceled", "vol_exec": "0.00000000",
         "cost": "0.00000", "fee": "0.00000", "price": "0.00000"},
        now=kr.datetime(2026, 9, 11, 7, 5, tzinfo=kr.CHICAGO_TZ),
    )
    adds = [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"]
    r.check("deadline prevents replacement AddOrder", adds, [])
    r.check("terminal zero fill becomes fallback-eligible",
            trace["row"]["status"], "canceled_unfilled")
    r.check("existing fallback path owns remainder", len(trace["fallback_calls"]), 1)


def t_recovery_partial_fill_is_persisted_before_remaining_action(r):
    trace = _run_recovery_cycle({
        "status": "canceled", "vol_exec": "58.50000000",
        "cost": "2.00000", "fee": "0.00800", "price": "0.03418",
    })
    adds = [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"]
    fill_updates = [u for _t, _f, u in trace["updates"]
                    if u.get("filled_quote_cost") == 2.0]
    r.check("partial cost is durably reconciled", len(fill_updates), 1)
    r.check("partial fee is durably reconciled", fill_updates[0]["fee_quote"], 0.008)
    r.check_true("remaining maker volume is smaller",
                 float(adds[0]["volume"]) < 291.23027)
    r.check_true("fill persistence precedes remaining-budget AddOrder",
                 trace["events"].index(("db", "raw"))
                 < trace["events"].index(("kraken", "AddOrder")))


def t_recovery_closed_fill_finalizes_without_another_spend(r):
    trace = _run_recovery_cycle({
        "status": "closed", "vol_exec": "290.00000000",
        "cost": "9.92000", "fee": "0.07900", "price": "0.03421",
    })
    r.check("closed original is finalized", len(trace["finalize_calls"]), 1)
    r.check("closed original creates no fallback", trace["fallback_calls"], [])
    r.check("closed original creates no replacement",
            [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"], [])


def t_recovery_ambiguous_provider_evidence_never_spends(r):
    cases = (
        ("missing", None, None),
        ("malformed", {"vol_exec": "0.00000000"}, None),
        ("api failure", None, RuntimeError("temporary QueryOrders failure")),
    )
    for name, observation, error in cases:
        trace = _run_recovery_cycle(observation, query_error=error)
        r.check(f"{name} remains recovery pending",
                trace["row"]["status"], kr.REPEG_RECOVERY_STATUS)
        r.check(f"{name} creates no replacement",
                [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"], [])
        r.check(f"{name} creates no fallback", trace["fallback_calls"], [])
        endpoints = [endpoint for endpoint, _params in trace["calls"]]
        r.check_true(f"{name} checks authoritative open-order source",
                     "OpenOrders" in endpoints)
        r.check_true(f"{name} checks authoritative closed-order source",
                     "ClosedOrders" in endpoints)

    incomplete_fill = _run_recovery_cycle({
        "status": "canceled", "vol_exec": "1.00000000",
    })
    r.check("incomplete positive fill remains recovery pending",
            incomplete_fill["row"]["status"], kr.REPEG_RECOVERY_STATUS)
    r.check("incomplete positive fill creates no replacement",
            [p for endpoint, p in incomplete_fill["calls"] if endpoint == "AddOrder"], [])
    r.check("incomplete positive fill creates no fallback",
            incomplete_fill["fallback_calls"], [])


def t_recovery_unresolved_at_frozen_ttl_requires_manual(r):
    trace = _run_recovery_cycle(
        None,
        now=kr.datetime(2026, 9, 11, 8, 10, tzinfo=kr.CHICAGO_TZ),
    )
    r.check("frozen TTL dead-letters unresolved recovery",
            trace["row"]["status"], "manual_required")
    r.check("TTL ambiguity creates no replacement",
            [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"], [])
    r.check("TTL ambiguity creates no fallback", trace["fallback_calls"], [])


def t_replacement_response_loss_is_not_blindly_retried(r):
    first = _run_recovery_cycle(
        {"status": "canceled", "vol_exec": "0.00000000",
         "cost": "0.00000", "fee": "0.00000", "price": "0.00000"},
        add_order_error=TimeoutError("response lost"),
    )
    first_adds = [p for endpoint, p in first["calls"] if endpoint == "AddOrder"]
    r.check("ambiguous submission was attempted once", len(first_adds), 1)
    r.check("response loss remains recovery pending",
            first["row"]["status"], kr.REPEG_RECOVERY_STATUS)

    second = _run_recovery_cycle(
        {"status": "canceled", "vol_exec": "0.00000000",
         "cost": "0.00000", "fee": "0.00000", "price": "0.00000"},
        row=first["row"],
    )
    r.check("later cycle does not blindly repeat AddOrder",
            [p for endpoint, p in second["calls"] if endpoint == "AddOrder"], [])
    r.check("unresolved replacement creates no fallback",
            second["fallback_calls"], [])
    r.check("unresolved replacement stays pending",
            second["row"]["status"], kr.REPEG_RECOVERY_STATUS)


def t_ambiguous_replacement_found_is_attached_or_finalized(r):
    pending = _recovery_row("replacement_submission_pending")
    next_cl = pending["raw"]["repeg_transition"]["replacement_cl_ord_id"]
    open_order = {
        "status": "open", "vol_exec": "0.00000000", "cl_ordid": next_cl,
    }
    attached = _run_recovery_cycle(
        row=pending,
        replacement_open={"OFOUND-OPEN": open_order},
    )
    r.check("found open replacement becomes limit_open",
            attached["row"]["status"], "limit_open")
    r.check("found open replacement provider id is attached",
            attached["row"]["order_id"], "OFOUND-OPEN")

    closed_order = {
        "status": "closed", "vol_exec": "291.00000000",
        "cost": "9.92000", "fee": "0.07900", "price": "0.03418",
        "cl_ordid": next_cl,
    }
    finalized = _run_recovery_cycle(
        row=_recovery_row("replacement_submission_pending"),
        replacement_closed={"OFOUND-CLOSED": closed_order},
    )
    r.check("found closed replacement is finalized",
            len(finalized["finalize_calls"]), 1)
    r.check("found closed replacement creates no new AddOrder",
            [p for endpoint, p in finalized["calls"] if endpoint == "AddOrder"], [])



def _generation_fill(generation, order_id, provider_cl, cost, fee, volume):
    return {
        "generation": generation,
        "provider_order_id": order_id,
        "provider_client_id": provider_cl,
        "filled_quote_cost": cost,
        "fee_quote": fee,
        "filled_base_volume": volume,
        "provider_status": "canceled",
        "observed_at": "2026-09-11T12:00:00+00:00",
    }


def _persisted_event_consumption(row):
    raw = kr._safe_json_load(row.get("raw")) or {}
    transition = raw.get("repeg_transition") or {}
    fills = transition.get("generation_fills") or []
    cost = sum(float(item["filled_quote_cost"]) for item in fills)
    fee = sum(float(item["fee_quote"]) for item in fills)
    remaining = float(row["requested_quote_amount_base"]) - cost - fee
    return fills, cost, fee, remaining


def t_original_partial_then_replacement_terminal_is_cumulative(r):
    row = _recovery_row("replacement_submission_pending")
    transition = row["raw"]["repeg_transition"]
    transition["generation_fills"] = [
        _generation_fill(0, row["order_id"], row["cl_ord_id"],
                         2.0, 0.008, 58.5)]
    row["filled_quote_cost"] = 2.0
    row["fee_quote"] = 0.008
    next_cl = transition["replacement_cl_ord_id"]
    replacement = {
        "status": "closed", "vol_exec": "87.77000000",
        "cost": "3.00000", "fee": "0.01200", "price": "0.03418",
        "cl_ordid": next_cl,
    }

    trace = _run_recovery_cycle(
        row=row,
        replacement_closed={"O-REPLACEMENT-CLOSED": replacement},
    )
    fills, cost, fee, remaining = _persisted_event_consumption(trace["row"])

    r.check("original plus replacement generations are retained", len(fills), 2)
    r.check("terminal replacement adds to prior cost", round(cost, 6), 5.0)
    r.check("terminal replacement adds to prior fee", round(fee, 6), 0.02)
    r.check("persisted row mirrors cumulative cost",
            trace["row"]["filled_quote_cost"], 5.0)
    r.check("persisted row mirrors cumulative fee",
            trace["row"]["fee_quote"], 0.02)
    r.check("persisted remaining follows scheduled authority",
            round(remaining, 6), 4.98)
    r.check("terminal replacement still reaches finalization",
            len(trace["finalize_calls"]), 1)


def _generation_two_recovery(prior_cost=1.0, prior_fee=0.004):
    row = _recovery_row()
    transition = row["raw"]["repeg_transition"]
    cl = row["cl_ord_id"]
    transition.update({
        "generation": 2,
        "original_order_id": "O-REPEG-R1",
        "original_provider_cl_ord_id": f"{cl}-r1",
        "replacement_cl_ord_id": f"{cl}-r2",
        "replacement_price": 0.03419,
    })
    transition["replacement_request"] = {
        **transition["replacement_request"],
        "price": "0.03419",
        "cl_ordid": f"{cl}-r2",
    }
    transition["generation_fills"] = [
        _generation_fill(0, "O-ORIGINAL", cl,
                         prior_cost, prior_fee, 29.0)]
    row.update({
        "order_id": "O-REPEG-R1",
        "limit_price": 0.03418,
        "filled_quote_cost": prior_cost,
        "fee_quote": prior_fee,
    })
    row["raw"]["kraken_cl"] = f"{cl}-r2"
    row["raw"]["repeg_count"] = 2
    return row


def t_generation_one_partial_to_generation_two_honors_persisted_budget(r):
    row = _generation_two_recovery()
    trace = _run_recovery_cycle({
        "status": "canceled", "vol_exec": "29.24830000",
        "cost": "1.00000", "fee": "0.00400", "price": "0.03418",
        "cl_ordid": f"{row['cl_ord_id']}-r1",
    }, row=row)
    persisted = kr.json.loads(kr.json.dumps(trace["row"]))
    fills, cost, fee, remaining = _persisted_event_consumption(persisted)
    adds = [params for endpoint, params in trace["calls"]
            if endpoint == "AddOrder"]

    r.check("generation 0 and 1 survive process boundary", len(fills), 2)
    r.check("process-restored cumulative cost", round(cost, 6), 2.0)
    r.check("process-restored cumulative fee", round(fee, 6), 0.008)
    r.check("process-restored remaining quote", round(remaining, 6), 7.992)
    r.check("generation 2 uses its durable identity",
            adds[0]["cl_ordid"], f"{row['cl_ord_id']}-r2")
    reserved = float(adds[0]["volume"]) * float(adds[0]["price"]) * 1.004
    r.check_true("cumulative cost and fees bound next spend",
                 cost + fee + reserved <= row["requested_quote_amount_base"])


def t_earlier_generation_fill_cannot_be_forgotten_and_overspent(r):
    row = _generation_two_recovery(prior_cost=8.5, prior_fee=0.034)
    trace = _run_recovery_cycle({
        "status": "canceled", "vol_exec": "29.24830000",
        "cost": "1.00000", "fee": "0.00400", "price": "0.03418",
        "cl_ordid": f"{row['cl_ord_id']}-r1",
    }, row=row)
    _fills, cost, fee, remaining = _persisted_event_consumption(trace["row"])

    r.check("all generations remain charged", round(cost + fee, 6), 9.538)
    r.check("only true event remainder survives", round(remaining, 6), 0.462)
    r.check("insufficient cumulative remainder cannot AddOrder",
            [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"], [])
    r.check("existing fallback receives the cumulative row",
            len(trace["fallback_calls"]), 1)
    fallback_row = trace["fallback_calls"][0][0][0] if trace["fallback_calls"] else {}
    r.check("fallback cost projection is cumulative",
            fallback_row["filled_quote_cost"], 9.5)
    r.check("fallback fee projection is cumulative",
            round(fallback_row["fee_quote"], 6), 0.038)


def t_recovery_attach_is_not_processed_twice_in_one_inspection(r):
    trace = _run_recovery_cycle({
        "status": "canceled", "vol_exec": "0.00000000",
        "cost": "0.00000", "fee": "0.00000", "price": "0.00000",
    }, include_transitioned_open=True)
    endpoints = [endpoint for endpoint, _params in trace["calls"]]

    r.check("recovery attaches replacement", trace["row"]["status"], "limit_open")
    r.check("one provider read owns this invocation",
            endpoints.count("QueryOrders"), 1)
    r.check("normal limit pass does not cancel attached replacement",
            endpoints.count("CancelOrder"), 0)


def t_cancel_readback_merges_and_preserves_recovery_envelope(r):
    row = _recovery_row("replacement_attached")
    row.update({"status": "limit_open", "order_id": "O-REPEG-R1"})
    raw = row["raw"]
    raw["repeg_history"] = [{"n": 1, "phase": "recovery_armed"}]
    transition = raw["repeg_transition"]
    transition["replacement_order_id"] = "O-REPEG-R1"
    transition["generation_fills"] = [
        _generation_fill(0, "O-ORIGINAL", row["cl_ord_id"],
                         1.0, 0.004, 29.0)]
    writes = []

    def fake_kraken(endpoint, params=None):
        if endpoint == "CancelOrder":
            return {"count": 1}
        if endpoint == "QueryOrders":
            return {"O-REPEG-R1": {
                "status": "canceled", "vol_exec": "0.00000000",
                "cost": "0.00000", "fee": "0.00000", "price": "0.00000",
                "cl_ordid": f"{row['cl_ord_id']}-r1",
            }}
        raise AssertionError(endpoint)

    def fake_update(table, filters, updates):
        writes.append((table, filters, updates))
        return [updates]

    originals = {
        "kraken_private": kr.kraken_private,
        "sb_update": kr.sb_update,
    }
    kr.kraken_private = fake_kraken
    kr.sb_update = fake_update
    try:
        kr._cancel_confirm_readback(row)
    finally:
        for name, value in originals.items():
            setattr(kr, name, value)

    persisted = kr._safe_json_load(writes[-1][2]["raw"]) or {}
    kept = persisted.get("repeg_transition") or {}
    r.check("recovery transition survives cancel readback",
            kept.get("request_fingerprint"), "test-fingerprint")
    r.check("replacement identity survives cancel readback",
            kept.get("replacement_cl_ord_id"), f"{row['cl_ord_id']}-r1")
    r.check_true("frozen deadlines survive cancel readback",
                 kept.get("maker_deadline") and kept.get("manual_at"))
    r.check("cumulative evidence survives cancel readback",
            len(kept.get("generation_fills") or []), 2)
    r.check("recovery history survives cancel readback",
            persisted.get("repeg_history"), [{"n": 1, "phase": "recovery_armed"}])


def t_generation_two_unknown_lookup_uses_current_provider_identity(r):
    row = _generation_two_recovery()
    trace = _run_recovery_cycle(
        None, row=row, query_error=RuntimeError("QueryOrders unavailable"))
    closed = [params for endpoint, params in trace["calls"]
              if endpoint == "ClosedOrders"]

    r.check("generation 2 fallback lookup uses generation 1 provider client",
            closed[0].get("cl_ordid"), f"{row['cl_ord_id']}-r1")
    r.check("stable DB identity remains unchanged",
            trace["row"]["cl_ord_id"], row["cl_ord_id"])
    r.check("unknown generation 1 creates no new spend",
            [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"], [])


def t_stale_recovery_cannot_dead_letter_advanced_row(r):
    transition = _recovery_row()["raw"]["repeg_transition"]
    manual_at = kr._repeg_time(transition["manual_at"])
    trace = _run_recovery_cycle(
        None, now=manual_at + kr.timedelta(seconds=1),
        advance_before_manual=True)
    manual_attempts = [filters for _table, filters, updates in trace["updates"]
                       if updates.get("status") == "manual_required"]

    r.check("concurrent owner remains authoritative",
            trace["row"]["status"], "limit_open")
    r.check("concurrent provider identity is retained",
            trace["row"]["order_id"], "O-ADVANCED")
    r.check("manual transition carried recovery ownership CAS",
            manual_attempts[0].get("status"),
            f"eq.{kr.REPEG_RECOVERY_STATUS}")
    r.check_true("manual transition protects the recovery phase",
                 "raw" in manual_attempts[0])


def t_recovery_ttl_equality_matches_existing_maker_boundary(r):
    transition = _recovery_row()["raw"]["repeg_transition"]
    manual_at = kr._repeg_time(transition["manual_at"])
    trace = _run_recovery_cycle(None, now=manual_at)

    r.check("exact TTL equality stays nonterminal",
            trace["row"]["status"], kr.REPEG_RECOVERY_STATUS)
    r.check("exact TTL equality does not spend",
            [p for endpoint, p in trace["calls"] if endpoint == "AddOrder"], [])
    r.check("exact TTL equality does not fallback", trace["fallback_calls"], [])


def t_replacement_projects_submit_time_market_telemetry(r):
    fresh = {"bid": 0.03419, "ask": 0.03423, "mid": 0.03421}
    trace = _run_recovery_cycle({
        "status": "canceled", "vol_exec": "0.00000000",
        "cost": "0.00000", "fee": "0.00000", "price": "0.00000",
    }, ticker_snapshot=fresh)

    transition = trace["row"]["raw"]["repeg_transition"]
    submit_market = transition["submission_market_snapshot"]
    r.check("replacement projects submit bid", trace["row"].get("bid"), fresh["bid"])
    r.check("replacement projects submit ask", trace["row"].get("ask"), fresh["ask"])
    r.check("replacement projects submit mid", trace["row"].get("mid"), fresh["mid"])
    r.check("replacement projects submit timestamp",
            trace["row"].get("mid_ts"), submit_market["observed_at"])
    r.check("recovery obtains one submit-time ticker", trace["ticker_calls"], ["KASUSD"])

TESTS = [
    ("happy path", t_happy_path),
    ("repeg_max reached", t_max_reached),
    ("repeg_max minus one", t_max_minus_one_allowed),
    ("repeg_max checked first", t_max_checked_first),
    ("bid not above resting price", t_bid_not_above),
    ("bid exactly at threshold", t_bid_exactly_at_threshold),
    ("min_ticks = 2", t_min_ticks_two),
    ("spread collapsed", t_spread_collapsed),
    ("bid equals ask", t_bid_equals_ask),
    ("cap vetoes", t_cap_vetoes),
    ("cap boundary allows", t_cap_boundary_allows),
    ("original and replacement fills stay cumulative",
     t_original_partial_then_replacement_terminal_is_cumulative),
    ("generation 2 honors persisted event budget",
     t_generation_one_partial_to_generation_two_honors_persisted_budget),
    ("earlier generation cannot be forgotten",
     t_earlier_generation_fill_cannot_be_forgotten_and_overspent),
    ("recovery row advances once per inspection",
     t_recovery_attach_is_not_processed_twice_in_one_inspection),
    ("cancel readback preserves recovery envelope",
     t_cancel_readback_merges_and_preserves_recovery_envelope),
    ("generation 2 uses current provider identity",
     t_generation_two_unknown_lookup_uses_current_provider_identity),
    ("manual recovery transition is ownership safe",
     t_stale_recovery_cannot_dead_letter_advanced_row),
    ("recovery TTL equality matches maker TTL",
     t_recovery_ttl_equality_matches_existing_maker_boundary),
    ("replacement projects submit-time telemetry",
     t_replacement_projects_submit_time_market_telemetry),
    ("cap missing reference", t_cap_missing_reference),
    ("H90 guard on the re-peg path", t_cap_h90_guard_reaches_repeg),
    ("below ordermin", t_below_ordermin),
    ("volume floors to zero", t_volume_zero),
    ("volume exactly ordermin", t_volume_exactly_ordermin),
    ("re-posts at the bid", t_returns_bid_not_ask),
    ("cancel/open readback requires observable continuation",
     t_cancel_open_readback_requires_observable_continuation),
    ("re-peg recovery armed before cancel",
     t_repeg_recovery_is_armed_before_cancel),
    ("re-peg cancel requires durable ownership",
     t_repeg_cancel_requires_successful_durable_claim),
    ("recovery writer/owner status consistency",
     t_recovery_writer_and_owner_share_status_constant),
    ("recovery open zero-fill waits",
     t_recovery_open_zero_fill_never_spends),
    ("recovery terminal zero-fill replaces",
     t_recovery_terminal_zero_fill_replaces_before_deadline),
    ("recovery terminal at deadline falls back",
     t_recovery_terminal_at_deadline_uses_existing_fallback),
    ("recovery partial fill before remaining action",
     t_recovery_partial_fill_is_persisted_before_remaining_action),
    ("recovery closed fill finalizes",
     t_recovery_closed_fill_finalizes_without_another_spend),
    ("recovery ambiguity never spends",
     t_recovery_ambiguous_provider_evidence_never_spends),
    ("recovery frozen TTL dead-letters",
     t_recovery_unresolved_at_frozen_ttl_requires_manual),
    ("replacement response loss is not retried",
     t_replacement_response_loss_is_not_blindly_retried),
    ("ambiguous replacement is attached or finalized",
     t_ambiguous_replacement_found_is_attached_or_finalized),
]

if __name__ == "__main__":
    raise SystemExit(Runner("_repeg_decision").run(TESTS))
