"""Same-day funding retries through the maker execution path, with fake I/O."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event, Lock
import io
import urllib.error
from unittest.mock import patch

from _harness import Runner, kr
from _rob21_world import MAKER_SETTINGS, ORDER, TODAY, World


SKIPPED = "skipped_insufficient_funds"
INITIAL_ORDER = {**ORDER, "target_time": "06:55", "time_window_minutes": 10}
NEW_ORDER = {**INITIAL_ORDER, "target_time": "07:45"}
NOW = datetime.fromisoformat(f"{TODAY}T06:53").replace(tzinfo=kr.CHICAGO_TZ)
REAL_WINDOW = kr._window_bounds_for
REAL_BALANCE = kr.check_balance_usd


class FundingWorld(World):
    def __init__(self):
        super().__init__()
        self.now = NOW
        self.usd = 3.90
        self.read_barrier = None
        self.update_lock = Lock()
        self.claims = 0

    def sb_get(self, table, params=None):
        rows = super().sb_get(table, params)
        if self.read_barrier and (params or {}).get("attempt_type") == "eq.maker_limit":
            self.read_barrier.wait(timeout=5)
        return rows

    def sb_insert(self, table, row):
        if table == "dca_executions":
            keys = ("dca_order_id", "trade_date_chicago", "attempt_type")
            if any(all(existing.get(key) == row.get(key) for key in keys)
                   for existing in self.rows):
                raise urllib.error.HTTPError("x", 409, "same daily leg", {}, io.BytesIO(b"{}"))
            row = {**row, "id": len(self.rows) + 1}
        return super().sb_insert(table, row)

    def sb_update(self, table, filters, updates):
        with self.update_lock:
            rows = super().sb_update(table, filters, updates)
            if updates.get("status") == "claimed":
                self.claims += len(rows)
            return rows

    def kraken_private(self, endpoint, params=None):
        if endpoint == "BalanceEx":
            self.kraken_calls.append((endpoint, params))
            return {"ZUSD": {"balance": str(self.usd), "hold_trade": "0"}}
        return super().kraken_private(endpoint, params)

    def cycle(self, order=NEW_ORDER):
        return kr.execute_pair(order, MAKER_SETTINGS, TODAY, "test-user")["status"]

    def seeded_skip(self):
        with self.installed(), self.clock():
            assert self.cycle(INITIAL_ORDER) == SKIPPED
        self.now += timedelta(minutes=50)
        return self.rows[0]

    def clock(self):
        world = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return world.now.astimezone(tz)

        return patch.multiple(kr, datetime=Clock, _window_bounds_for=REAL_WINDOW,
                              check_balance_usd=REAL_BALANCE)


def t_funded(r):
    w = FundingWorld()
    snapshots = []
    with w.installed(), w.clock(), patch.object(kr, "snapshot_mirror", snapshots.append):
        r.check("first check skips", w.cycle(INITIAL_ORDER), SKIPPED)
        row = w.rows[0]
        identity = (row["id"], row["cl_ord_id"], row["parent_event_id"])
        w.now += timedelta(minutes=50)
        w.usd += 10
        status = w.cycle()
        next_status = w.cycle()
    r.check("first skip and funded retry both snapshot", snapshots, ["test-user"] * 2)
    r.check("maker order opens once", (status, next_status), ("limit_open", "already_claimed"))
    r.check("existing row reused", [(x["id"], x["cl_ord_id"], x["parent_event_id"])
                                    for x in w.rows], [identity])
    r.check("BalanceEx refreshed", len(w.calls("BalanceEx")), 2)
    r.check("one post-only buy", [p["oflags"] for p in w.calls("AddOrder")], ["post,fciq"])


def t_still_short(r):
    w = FundingWorld()
    row = w.seeded_skip()
    initial_reason = row["reason"]
    # Historical skip rows predate the counter.
    raw = w.raw(row["cl_ord_id"])
    raw.pop("skip_count", None)
    row["raw"] = kr.json.dumps(raw)
    w.usd = 4.10
    with w.installed(), w.clock():
        r.check("second funding check skips", w.cycle(), SKIPPED)
    raw = w.raw(row["cl_ord_id"])
    r.check("two skips on one row", (len(w.rows), raw.get("skip_count")), (1, 2))
    r.check("previous reason survives", raw.get("previous_skip_reasons"), [initial_reason])
    r.check("new reason recorded", "USD $4.10" in row["reason"], True)
    r.check("no order submitted", w.calls("AddOrder"), [])


def t_notify_only_on_first_skip(r):
    w = FundingWorld()
    snapshots = []
    with w.installed(), w.clock(), patch.object(kr, "snapshot_mirror", snapshots.append):
        statuses = [w.cycle(INITIAL_ORDER)]
        w.now += timedelta(minutes=50)
        statuses.append(w.cycle())
        w.now += timedelta(minutes=5)
        statuses.append(w.cycle())

    r.check("all three balance checks skip", statuses, [SKIPPED] * 3)
    r.check("all three skips counted", w.raw(w.rows[0]["cl_ord_id"])["skip_count"], 3)
    r.check("one DCA SKIP notification", sum("DCA SKIP" in msg for msg in w.tg), 1)
    r.check("one mirror snapshot", snapshots, ["test-user"])


def t_notify_once_across_allowed_refusal(r):
    w = FundingWorld()
    snapshots = []
    w.endpoint_errors["AddOrder"] = kr.KrakenError(["EOrder:Insufficient funds"])
    with w.installed(), w.clock(), patch.object(kr, "snapshot_mirror", snapshots.append):
        first = w.cycle(INITIAL_ORDER)
        w.now += timedelta(minutes=50)
        w.usd += 10
        refused = w.cycle()
        w.now += timedelta(minutes=1)
        w.usd -= 10
        last = w.cycle()

    r.check("normal refusal retry still runs", (first, refused, last),
            (SKIPPED, "failed_kraken", SKIPPED))
    r.check("one funding skip message across attempts", sum("DCA SKIP" in msg for msg in w.tg), 1)
    r.check("skip and funded Kraken refusal both snapshot", snapshots, ["test-user"] * 2)


def t_notify_when_target_changes(r):
    w = FundingWorld()
    w.seeded_skip()
    snapshots = []
    new_target = {**NEW_ORDER, "base_quote_amount": 12.0}
    with w.installed(), w.clock(), patch.object(kr, "snapshot_mirror", snapshots.append):
        r.check("changed target still skips", w.cycle(new_target), SKIPPED)
        w.now += timedelta(minutes=1)
        r.check("same target still skips", w.cycle(new_target), SKIPPED)
    notices = [msg for msg in w.tg if "DCA SKIP" in msg]
    r.check("first notice and one changed-target notice", len(notices), 2)
    r.check("latest notice contains new target", "$12.00" in notices[-1], True)
    r.check("repeat skips never snapshot", snapshots, [])


def t_invalid_skip_count(r):
    for invalid in ("2", None, [1], True, -1, 1.5):
        w = FundingWorld()
        row = w.seeded_skip()
        raw = w.raw(row["cl_ord_id"])
        row["raw"] = kr.json.dumps({**raw, "skip_count": invalid})
        with w.installed(), w.clock():
            try:
                result = w.cycle()
            except TypeError as exc:
                result = str(exc)
        r.check(f"skip succeeds with invalid count {invalid!r}", result, SKIPPED)
        r.check("invalid count restarts at one", w.raw(row["cl_ord_id"])["skip_count"], 1)
        r.check("invalid count does not hide the notice", sum("DCA SKIP" in msg for msg in w.tg), 2)


def t_unsafe_to_retry(r):
    for status, order_id, raw in (
        ("filled", "OFILLED", {}),
        ("placed", "OPLACED", {}),
        ("limit_open", "OOPEN", {}),
        ("claimed", None, {"submission_state": "UNKNOWN"}),
        ("failed_kraken", None, {"error": "connection timed out"}),
        (SKIPPED, "OEXISTS", {}),
        (SKIPPED, None, {"txid": ["OEXISTS"]}),
        (SKIPPED, None, {"order_id": "OEXISTS"}),
        (SKIPPED, None, {"submission_state": "UNKNOWN"}),
    ):
        w = FundingWorld()
        row = w.seeded_skip()
        row.update(status=status, order_id=order_id, raw=kr.json.dumps(raw),
                   reason=raw.get("error"))
        w.usd += 10
        with w.installed(), w.clock():
            r.check(f"{status}/{order_id}/{raw} stays untouched", w.cycle(), "already_claimed")
        r.check("no extra BalanceEx, claim or AddOrder",
                (len(w.calls("BalanceEx")), w.claims, w.calls("AddOrder")), (1, 0, []))


def t_parallel(r):
    w = FundingWorld()
    w.seeded_skip()
    w.usd += 10
    w.read_barrier = Barrier(2)
    with w.installed(), w.clock(), ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: w.cycle(), range(2)))
    r.check("one claimant wins", sorted(statuses), ["already_claimed", "limit_open"])
    r.check("one claim and order", (w.claims, len(w.calls("AddOrder"))), (1, 1))


def t_stale_reader(r):
    w = FundingWorld()
    row = w.seeded_skip()
    stale_read = Event()
    winner_finished = Event()
    real_get = w.sb_get

    def pause_after_read(table, params=None):
        rows = real_get(table, params)
        if not stale_read.is_set():
            stale_read.set()
            if not winner_finished.wait(timeout=5):
                raise AssertionError("winner did not return the row to skip")
        return rows

    with patch.object(w, "sb_get", pause_after_read):
        with w.installed(), w.clock(), ThreadPoolExecutor(max_workers=2) as pool:
            stale = pool.submit(w.cycle)
            try:
                assert stale_read.wait(timeout=5)
                r.check("winner returns to skip", w.cycle(), SKIPPED)
            finally:
                winner_finished.set()
            r.check("stale cycle loses", stale.result(), "already_claimed")
    r.check("only the winner checked funds", (w.claims, len(w.calls("BalanceEx"))), (1, 2))
    r.check("one extra skip", w.raw(row["cl_ord_id"])["skip_count"], 2)
    r.check("no order submitted", w.calls("AddOrder"), [])


def t_window(r):
    for target in ("08:45", "25:45", "bad", "", 745):
        w = FundingWorld()
        row = w.seeded_skip()
        w.usd += 10
        order = {**NEW_ORDER, "target_time": target}
        with w.installed(), w.clock():
            r.check(f"{target!r} fails closed", w.cycle(order),
                    "outside_window" if target == "08:45" else "invalid_window")
        r.check(f"{target!r} leaves skip untouched", row["status"], SKIPPED)
        r.check("no extra BalanceEx, claim or AddOrder",
                (len(w.calls("BalanceEx")), w.claims, w.calls("AddOrder")), (1, 0, []))


def t_scheduled_cycle(r):
    w = FundingWorld()
    w.seeded_skip()
    w.usd += 10

    def scheduled_get(table, params=None):
        if table == "dca_settings":
            return [{**MAKER_SETTINGS, "user_id": "test-user"}]
        if table == "dca_orders":
            return [{**NEW_ORDER, "target_time": "bad"}, NEW_ORDER]
        return w.sb_get(table, params)

    with w.installed(), w.clock(), patch.multiple(
        kr, sb_get=scheduled_get, process_dca_commands=lambda *_: None,
        run_reconciliation=lambda *_: None, run_maker_inspection=lambda *_: None,
        send_weekly_summary=lambda *_: None,
    ), patch.object(kr.sys, "argv", ["kraken_run.py"]):
        kr.main()
    r.check("invalid order leaves the valid rescheduled order runnable",
            (w.claims, len(w.calls("BalanceEx")), len(w.calls("AddOrder"))), (1, 2, 1))


if __name__ == "__main__":
    raise SystemExit(Runner("Same-day funding retry").run([
        ("funds arrive in the rescheduled window", t_funded),
        ("funds are still short", t_still_short),
        ("only the first funding skip notifies and snapshots", t_notify_only_on_first_skip),
        ("a Kraken refusal does not reset the funding notice", t_notify_once_across_allowed_refusal),
        ("changed funding target notifies again", t_notify_when_target_changes),
        ("invalid skip counts recover", t_invalid_skip_count),
        ("existing or ambiguous Kraken orders are never retried", t_unsafe_to_retry),
        ("parallel cycles compete for one claim", t_parallel),
        ("stale reader loses after winner skips again", t_stale_reader),
        ("outside or invalid target window", t_window),
        ("scheduled cycle continues past invalid order", t_scheduled_cycle),
    ]))
