"""Gate B, all I/O mocked. Every reporting attempt uses fixed Chicago time.

The September 6–12 regression uses the exact approved production-derived
report-generation snapshot. All other synthetic edge-case tests remain.
This fixture covers reporting grain and economics, not re-peg execution.
"""
import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import Mock, patch

from _harness import kr
from weekly_summary import weekly_summary_evidence

NOW = datetime(2026, 9, 13, 6, 55, tzinfo=kr.CHICAGO_TZ)
WEEK = "2026-W37"


def row(status="filled", parent="p1", **extra):
    result = {
        "parent_event_id": parent, "pair": "KASUSD", "status": status,
        "trade_date_chicago": NOW.date().isoformat(),
        "attempt_type": "maker_limit", "cl_ord_id": "dca-KASUSD-live",
        "filled_quote_cost": None, "fee_quote": None,
        "filled_base_volume": None, "avg_price": None, "mid": None,
    }
    if status == "filled":
        result.update(filled_quote_cost=9.96, fee_quote=0.04,
                      filled_base_volume=285.0, avg_price=0.035, mid=0.034)
    result.update(extra)
    return result


class WeeklySummaryTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.markers, self.messages, self.queries = [], {}, [], []
        self.fail_get = self.fail_insert = self.fail_send = False
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        clock = self.enterContext(patch.object(kr, "datetime"))
        clock.now.return_value = NOW
        self.clock = clock
        self.enterContext(patch.object(kr, "sb_get", side_effect=self.get))
        self.enterContext(patch.object(kr, "sb_insert", side_effect=self.insert))
        self.send = self.enterContext(patch.object(kr, "_weekly_tg_send", side_effect=self.deliver))
        self.update = self.enterContext(patch.object(kr, "sb_update", side_effect=AssertionError("execution mutation")))
        self.exchange = self.enterContext(patch.object(kr, "kraken_private", side_effect=AssertionError("live exchange")))

    def tearDown(self):
        self.update.assert_not_called()
        self.exchange.assert_not_called()

    def get(self, table, params):
        self.queries.append((table, copy.deepcopy(params)))
        if table == "dca_executions":
            # Emulate exactly the existing lower-bound-only filter.
            lo = params["trade_date_chicago"].removeprefix("gte.")
            return [r for r in self.rows if r["trade_date_chicago"] >= lo]
        key = (params["notification_type"].removeprefix("eq."),
               params["period_key"].removeprefix("eq."))
        if self.fail_get and key[0] == "weekly_summary_blocked":
            raise RuntimeError("DB unavailable")
        return [self.markers[key]] if key in self.markers else []

    def insert(self, table, payload):
        self.assertEqual(table, "dca_notifications")
        if self.fail_insert:
            raise RuntimeError("DB unavailable")
        key = (payload["notification_type"], payload["period_key"])
        if key in self.markers:
            raise RuntimeError("unique conflict")
        self.markers[key] = copy.deepcopy(payload)
        return [payload]

    def deliver(self, text):
        self.messages.append(text)
        if self.fail_send:
            raise RuntimeError("Telegram unavailable")

    def attempt(self):
        before = copy.deepcopy(self.rows)
        kr.send_weekly_summary("test-user")
        self.assertEqual(self.rows, before)
        self.clock.now.assert_called_with(kr.CHICAGO_TZ)

    def stats(self, rows, counts=(1, 0, 0)):
        pairs, blockers = weekly_summary_evidence(rows)
        self.assertEqual(blockers, {})
        stats = pairs["KASUSD"]
        self.assertEqual(tuple(stats[k] for k in ("filled", "skipped", "failed")), counts)
        return stats

    def blocked(self, rows):
        pairs, blockers = weekly_summary_evidence(rows)
        self.assertEqual(pairs, {})
        self.assertTrue(blockers)

    def test_direct_maker_fill(self):
        self.stats([row()])

    def test_canceled_maker_fallback(self):
        stats = self.stats([row("canceled_unfilled", reason="fallback_created"),
                            row(attempt_type="maker_fallback")])
        self.assertEqual(stats["total_cost"] + stats["total_fee"], 10)

    def test_synthetic_rejected_postonly_fallback(self):
        self.stats([row("rejected_postonly"), row(attempt_type="maker_fallback")])

    def test_null_and_zero_non_economic_leg(self):
        nulls = row("canceled_unfilled", reason="fallback_created")
        zeros = dict(nulls, filled_quote_cost=0.0, fee_quote=0.0, filled_base_volume=0.0)
        self.assertEqual(self.stats([nulls, row()]), self.stats([zeros, row()]))

    def test_all_policy_skips(self):
        for status in ("skipped_insufficient_funds", "skipped_above_cap",
                       "skipped_target_too_small", "skipped_min_order"):
            with self.subTest(status=status):
                self.stats([row(status)], (0, 1, 0))
                self.stats([row(status), row()])

    def test_terminal_failure_evidence(self):
        self.stats([row("failed_reconciliation",
                        reason="Claimed but no Kraken order found after timeout",
                        execution_finished_at="2026-09-13T12:00:00Z")], (0, 0, 1))
        self.stats([row("failed_kraken", raw={"error": "EOrder:Insufficient funds"},
                        execution_finished_at="2026-09-13T12:00:00Z")], (0, 0, 1))

    def test_status_alone_does_not_prove_failure(self):
        for status in ("failed_kraken", "failed_reconciliation", "manual_required"):
            self.blocked([row(status)])
            self.blocked([row(status), row()])
        self.blocked([row("manual_required", reason="cancel not confirmed",
                          execution_finished_at="2026-09-13T12:00:00Z", order_id="live")])

    def test_positive_dry_fills_excluded_first(self):
        dry = row(parent=None, status="filled_dry_run", raw="invalid json")
        self.assertEqual(weekly_summary_evidence([dry]), ({}, {}))
        self.rows = [dry]
        self.attempt()
        self.assertEqual((self.messages, self.markers), ([], {}))

    def test_shared_status_dry_provenance(self):
        for provenance in ({"cl_ord_id": "dca-KASUSD-dry-fb"},
                           {"raw": {"dry_run": True}},
                           {"raw": json.dumps({"dry_run": True})}):
            for status in ("filled", "limit_open", "manual_required"):
                self.assertEqual(weekly_summary_evidence([
                    row(parent=None, status=status, **provenance)]), ({}, {}))
        self.stats([row(raw={"dry_run": False})])

    def test_current_day_limit_open_blocks_once(self):
        self.rows = [row("limit_open")]
        self.assertEqual(self.rows[0]["trade_date_chicago"], NOW.date().isoformat())
        self.attempt()
        self.attempt()
        self.assertEqual(set(self.markers), {("weekly_summary_blocked", WEEK)})
        self.assertEqual(len(self.messages), 1)
        self.assertIn("unresolved parents: 1 (2026-09-13)", self.messages[0])
        for forbidden in ("$", "KAS", "Avg", "slippage", "all-in"):
            self.assertNotIn(forbidden, self.messages[0])

    def test_past_day_unresolved(self):
        for status in ("claimed", "placed", "limit_open", "repeg_recovery_pending"):
            self.rows = [row(status, trade_date_chicago="2026-09-12")]
            self.attempt()
        self.assertNotIn(("weekly_summary", WEEK), self.markers)
        self.assertEqual(len(self.messages), 1)

    def test_null_parent(self):
        self.rows = [row(parent=None)]
        self.attempt()
        self.assertIn("null-parent rows: 1 (2026-09-13)", self.messages[0])
        self.assertNotIn(("weekly_summary", WEEK), self.markers)

    def test_synthetic_unresolved_partial(self):
        self.rows = [row("canceled_partial", filled_quote_cost=3, fee_quote=0.01,
                         filled_base_volume=90)]
        self.attempt()
        self.assertIn("unadjudicated-partial parents: 1 (2026-09-13)", self.messages[0])
        self.assertNotIn(("weekly_summary", WEEK), self.markers)

    def test_blocked_idempotency_retains_first_reason(self):
        self.rows = [row("limit_open")]
        self.attempt()
        first = copy.deepcopy(self.markers)
        self.rows[0]["status"] = "canceled_partial"
        for _ in range(3):
            self.attempt()
        self.assertEqual(self.markers, first)
        self.assertEqual(self.send.call_count, 1)

    def test_blocked_then_same_parent_success(self):
        self.rows = [row("limit_open")]
        self.attempt()
        self.assertEqual(set(self.markers), {("weekly_summary_blocked", WEEK)})
        prior = copy.deepcopy(self.markers[("weekly_summary_blocked", WEEK)])
        self.rows[0].update(row())  # SAME parent and date; evidence really changes.
        self.attempt()
        self.attempt()
        self.assertEqual(set(self.markers), {("weekly_summary_blocked", WEEK),
                                            ("weekly_summary", WEEK)})
        self.assertEqual(self.markers[("weekly_summary_blocked", WEEK)], prior)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("1 filled", self.messages[-1])

    def test_w37_production_derived_regression(self):
        # Historical snapshot at approximately 06:03 Chicago, before the
        # September 13 execution began around 06:53. No September 13 row belongs
        # here: the unchanged Step-1 query has a lower bound only.
        self.clock.now.return_value = datetime(2026, 9, 13, 6, 3, tzinfo=kr.CHICAGO_TZ)
        # Exact supplied values, including NULL vs 0.0 and full mid precision.
        # No raw.repeg_transition: this is reporting coverage, not re-peg coverage.
        self.rows = [
            {
                "trade_date_chicago": "2026-09-06",
                "pair": "KASUSD",
                "status": "filled",
                "reason": None,
                "attempt_type": "maker_limit",
                "cl_ord_id": "dca-KASUSD-2026-09-06-704",
                "parent_event_id": "9962c29b-83e9-44e8-a741-655f0b93c325",
                "filled_quote_cost": 9.96016,
                "fee_quote": 0.03984,
                "filled_base_volume": 328.39298,
                "avg_price": 0.03033,
                "mid": 0.03034,
            },
            {
                "trade_date_chicago": "2026-09-07",
                "pair": "KASUSD",
                "status": "filled",
                "reason": None,
                "attempt_type": "maker_limit",
                "cl_ord_id": "dca-KASUSD-2026-09-07-704",
                "parent_event_id": "cc143de0-7977-41c9-b248-eb4ca4f44486",
                "filled_quote_cost": 9.96016,
                "fee_quote": 0.03984,
                "filled_base_volume": 283.44221,
                "avg_price": 0.03514,
                "mid": 0.03516,
            },
            {
                "trade_date_chicago": "2026-09-08",
                "pair": "KASUSD",
                "status": "filled",
                "reason": None,
                "attempt_type": "maker_limit",
                "cl_ord_id": "dca-KASUSD-2026-09-08-704",
                "parent_event_id": "5493d296-e8a1-42c3-8a2d-ae8cf766a1fe",
                "filled_quote_cost": 9.96016,
                "fee_quote": 0.03984,
                "filled_base_volume": 288.03237,
                "avg_price": 0.03458,
                "mid": 0.034605,
            },
            {
                "trade_date_chicago": "2026-09-09",
                "pair": "KASUSD",
                "status": "filled",
                "reason": None,
                "attempt_type": "maker_limit",
                "cl_ord_id": "dca-KASUSD-2026-09-09-704",
                "parent_event_id": "28b99f61-2be4-4027-8af2-4f7a49c42777",
                "filled_quote_cost": 9.96016,
                "fee_quote": 0.03984,
                "filled_base_volume": 273.18045,
                "avg_price": 0.03646,
                "mid": 0.036485000000000004,
            },
            {
                "trade_date_chicago": "2026-09-10",
                "pair": "KASUSD",
                "status": "filled",
                "reason": None,
                "attempt_type": "maker_limit",
                "cl_ord_id": "dca-KASUSD-2026-09-10-704",
                "parent_event_id": "3ccb106a-cbd5-4e09-a2e1-5140bff8da56",
                "filled_quote_cost": 9.96016,
                "fee_quote": 0.03984,
                "filled_base_volume": 258.30288,
                "avg_price": 0.03856,
                "mid": 0.03858,
            },
            {
                "trade_date_chicago": "2026-09-11",
                "pair": "KASUSD",
                "status": "canceled_unfilled",
                "reason": "fallback_created",
                "attempt_type": "maker_limit",
                "cl_ord_id": "dca-KASUSD-2026-09-11-704",
                "parent_event_id": "0ef48173-95a2-45ba-a406-38d7652a32c6",
                "filled_quote_cost": None,
                "fee_quote": None,
                "filled_base_volume": None,
                "avg_price": None,
                "mid": 0.034155000000000005,
            },
            {
                "trade_date_chicago": "2026-09-11",
                "pair": "KASUSD",
                "status": "filled",
                "reason": None,
                "attempt_type": "maker_fallback",
                "cl_ord_id": "dca-KASUSD-2026-09-11-704-fb",
                "parent_event_id": "0ef48173-95a2-45ba-a406-38d7652a32c6",
                "filled_quote_cost": 9.92063,
                "fee_quote": 0.07937,
                "filled_base_volume": 289.9075,
                "avg_price": 0.03421,
                "mid": 0.0342,
            },
            {
                "trade_date_chicago": "2026-09-12",
                "pair": "KASUSD",
                "status": "canceled_unfilled",
                "reason": "fallback_created",
                "attempt_type": "maker_limit",
                "cl_ord_id": "dca-KASUSD-2026-09-12-704",
                "parent_event_id": "ecbd1dc4-5033-4f59-bc05-16d632fdc0da",
                "filled_quote_cost": 0.0,
                "fee_quote": 0.0,
                "filled_base_volume": 0.0,
                "avg_price": None,
                "mid": 0.03556,
            },
            {
                "trade_date_chicago": "2026-09-12",
                "pair": "KASUSD",
                "status": "filled",
                "reason": None,
                "attempt_type": "maker_fallback",
                "cl_ord_id": "dca-KASUSD-2026-09-12-704-fb",
                "parent_event_id": "ecbd1dc4-5033-4f59-bc05-16d632fdc0da",
                "filled_quote_cost": 9.92063,
                "fee_quote": 0.07937,
                "filled_base_volume": 279.84865,
                "avg_price": 0.03544,
                "mid": 0.035435,
            },
        ]
        self.assertEqual(len(self.rows), 9)
        self.assertEqual(len({r["parent_event_id"] for r in self.rows}), 7)
        self.assertEqual({r["trade_date_chicago"] for r in self.rows},
                         {f"2026-09-{day:02}" for day in range(6, 13)})
        stats = self.stats(self.rows, (7, 0, 0))
        self.assertEqual(sum(stats[k] for k in ("filled", "skipped", "failed")), 7)
        # Only the seven economic fills contribute to the existing arithmetic
        # mean. The two canceled rows have mids but no average fill price.
        expected_slippages = [
            (avg - mid) / mid * 100 for avg, mid in (
                (0.03033, 0.03034),
                (0.03514, 0.03516),
                (0.03458, 0.034605),
                (0.03646, 0.036485000000000004),
                (0.03856, 0.03858),
                (0.03421, 0.0342),
                (0.03544, 0.035435),
            )
        ]
        self.assertEqual(len(stats["slippages"]), 7)
        self.assertEqual(stats["slippages"], expected_slippages)
        all_in = stats["total_cost"] + stats["total_fee"]
        self.assertEqual(f"{all_in:.2f}", "70.00")
        self.assertEqual(f"{stats['total_vol']:.6f}", "2001.107040")
        self.assertEqual(f"{all_in / stats['total_vol']:.6f}", "0.034981")
        self.attempt()
        self.assertIn("7 filled", self.messages[0])
        self.assertIn("$70.00 all-in | 2001.107040 KAS", self.messages[0])
        self.assertIn("Avg price: $0.034981", self.messages[0])
        expected_slippage_mean = sum(expected_slippages) / 7
        self.assertIn(f"Avg slippage: {expected_slippage_mean:.4f}%", self.messages[0])
        query = next(params for table, params in self.queries if table == "dca_executions")
        self.assertEqual(query["trade_date_chicago"], "gte.2026-09-06")
        self.assertEqual(query["order"], "trade_date_chicago.asc")
        self.assertNotIn("and", query)
        self.assertEqual(self.markers[("weekly_summary", WEEK)]["period_key"], WEEK)

    def test_canceled_unfilled_economics_with_filled_sibling_blocks(self):
        for component in ("filled_quote_cost", "fee_quote", "filled_base_volume"):
            with self.subTest(component=component):
                closed = row("canceled_unfilled", **{component: 1})
                pairs, blockers = weekly_summary_evidence([closed, row()])
                self.assertEqual(pairs, {})
                self.assertEqual(blockers["ambiguous-purchase parents"]["count"], 1)

    def test_rejected_postonly_economics_with_filled_sibling_blocks(self):
        closed = row("rejected_postonly", filled_quote_cost=1, fee_quote=0.01,
                     filled_base_volume=30)
        pairs, blockers = weekly_summary_evidence([closed, row()])
        self.assertEqual(pairs, {})
        self.assertEqual(blockers["ambiguous-purchase parents"]["count"], 1)

    def test_slippage_only_economic_real_rows(self):
        fill = row()
        expected = (0.035 - 0.034) / 0.034 * 100
        stats = self.stats([fill, row("canceled_unfilled", avg_price=999, mid=1),
                            row(parent="dry", status="filled_dry_run", avg_price=999, mid=1)])
        self.assertEqual(stats["slippages"], [expected])

    def test_lifecycle_alone_and_unknown_statuses_block(self):
        for status in ("claimed", "placed", "limit_open", "repeg_recovery_pending",
                       "canceled_unfilled", "rejected_postonly", "mystery",
                       "not_really_skipped", "not_really_filled"):
            self.blocked([row(status)])
        self.blocked([row(), row("limit_open")])

    def test_invalid_economics_and_conflicting_pair(self):
        for amount in (-1, "nan", "inf", "bad"):
            self.blocked([row(filled_quote_cost=amount)])
        self.blocked([row(filled_base_volume=0)])
        self.blocked([row(), row(pair="BTCUSD")])
        self.blocked([row("skipped_min_order", filled_quote_cost=1)])

    def test_blocked_get_insert_and_send_failures_are_isolated(self):
        for failure in ("fail_get", "fail_insert", "fail_send"):
            with self.subTest(failure=failure):
                self.markers.clear()
                self.messages.clear()
                setattr(self, failure, True)
                self.rows = [row("limit_open")]
                self.attempt()
                self.assertNotIn(("weekly_summary", WEEK), self.markers)
                self.assertIn("reporting failed", self.output.getvalue())
                if failure == "fail_send":
                    self.assertNotIn(("weekly_summary_blocked", WEEK), self.markers)
                    self.assertEqual(len(self.messages), 1)
                    self.fail_send = False
                    self.attempt()
                    self.assertIn(("weekly_summary_blocked", WEEK), self.markers)
                    self.assertEqual(len(self.messages), 2)
                else:
                    if failure == "fail_get":
                        self.assertEqual(self.messages, [])
                    else:
                        self.assertEqual(len(self.messages), 1)
                setattr(self, failure, False)

    def test_concurrent_blocked_insert_conflict_can_send_before_conflict(self):
        self.rows = [row("limit_open")]
        error = kr.urllib.error.HTTPError("mock", 409, "conflict", {}, io.BytesIO())
        try:
            with patch.object(kr, "sb_insert", side_effect=error):
                self.attempt()
        finally:
            error.close()
        self.assertEqual(len(self.messages), 1)

    def test_unconfirmed_blocked_insert_happens_after_send(self):
        self.rows = [row("limit_open")]
        with patch.object(kr, "sb_insert", return_value=None):
            self.attempt()
        self.assertEqual(len(self.messages), 1)

    def test_blocker_dates_are_sorted_unique_from_classified_rows(self):
        pairs, blockers = weekly_summary_evidence([
            row("limit_open", parent="one", trade_date_chicago="2026-09-12"),
            row("placed", parent="one", trade_date_chicago="2026-09-11"),
            row("claimed", parent="two", trade_date_chicago="2026-09-11"),
        ])
        self.assertEqual(pairs, {})
        self.assertEqual(blockers, {
            "unresolved parents": {
                "count": 2,
                "dates": ["2026-09-11", "2026-09-12"],
            },
        })

    def test_invalid_provenance_retains_available_date(self):
        bad = row(parent=None, raw="invalid json", trade_date_chicago="2026-09-11")
        self.assertEqual(weekly_summary_evidence([bad]), ({}, {
            "invalid-provenance rows": {"count": 1, "dates": ["2026-09-11"]},
        }))

    def test_read_model_exception_isolated(self):
        self.rows = [row()]
        with patch.object(kr, "weekly_summary_evidence", side_effect=RuntimeError("broken")):
            self.attempt()
        self.assertEqual(self.markers, {})
        self.assertIn("reporting failed", self.output.getvalue())

    def test_normal_marker_only_after_successful_send(self):
        self.rows = [row()]
        self.fail_send = True
        self.attempt()
        self.assertEqual(self.markers, {})
        self.fail_send = False
        self.attempt()
        self.assertIn(("weekly_summary", WEEK), self.markers)

    def test_distinct_parents_same_date_remain_distinct(self):
        self.stats([row(parent="first"), row(parent="second")], (2, 0, 0))

    def test_one_blocker_suppresses_other_parents_totals(self):
        self.rows = [row(parent="good"), row("limit_open", parent="pending")]
        self.attempt()
        self.assertEqual(set(self.markers), {("weekly_summary_blocked", WEEK)})
        self.assertEqual(len(self.messages), 1)
        self.assertNotIn("$", self.messages[0])
        self.assertNotIn("filled", self.messages[0])

    def test_terminal_skip_with_closed_predecessor(self):
        self.stats([row("canceled_unfilled"), row("skipped_above_cap")], (0, 1, 0))

    def test_execution_query_failure_isolated(self):
        with patch.object(kr, "sb_get", side_effect=RuntimeError("DB unavailable")):
            self.attempt()
        self.assertEqual(self.markers, {})
        self.assertEqual(self.messages, [])
        self.assertIn("reporting failed", self.output.getvalue())

    def test_saturday_still_skips(self):
        self.clock.now.return_value = NOW.replace(day=12)
        self.rows = [row()]
        self.attempt()
        self.assertEqual(self.queries, [])
        self.assertEqual(self.messages, [])


class WeeklyTelegramTests(unittest.TestCase):
    def test_delivery_requires_confirmed_response(self):
        for reply in ({"ok": True}, {"ok": False}):
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = json.dumps(reply).encode()
            with patch.object(kr, "TG_BOT_TOKEN", "test-token"), \
                    patch.object(kr, "TG_CHAT_ID", "test-chat"), \
                    patch.object(kr.urllib.request, "urlopen", return_value=response):
                if reply["ok"]:
                    kr._weekly_tg_send("summary")
                else:
                    with self.assertRaises(RuntimeError):
                        kr._weekly_tg_send("summary")

    def test_unconfigured_delivery_is_not_success(self):
        with patch.object(kr, "TG_BOT_TOKEN", ""):
            with self.assertRaises(RuntimeError):
                kr._weekly_tg_send("summary")


if __name__ == "__main__":
    unittest.main()
