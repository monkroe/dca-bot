#!/usr/bin/env python3
"""Zero-spend retry paths against the ROB-21 fake; no network or credentials."""
import io
import json
import urllib.error

from _harness import kr, Runner
from _rob21_world import World, MAKER_SETTINGS, ORDER, TODAY, CL

PID = kr.provider_client_id("maker_limit", CL, 0)


def seed_skip(w, count=None):
    raw = {"kraken_cl": PID, "spendable_usd": 3.90, "needed_usd": 10.0,
           "held_usd": 470.0, "balance_source": "BalanceEx",
           "at": "2026-10-06T12:00:00+00:00"}
    if count is not None:
        raw["skip_count"] = count
    w.add_row(id=7, cl_ord_id=CL, status="skipped_insufficient_funds",
              attempt_type="maker_limit", dca_order_id=1, parent_event_id="evt-1",
              order_id=None, filled_quote_cost=None, fee_quote=None, raw=raw)


def indexed_world(w):
    """Emulate the production dca_exec_leg_per_event_uniq (fake omits it)."""
    original = w.sb_insert

    def insert(table, row):
        if table == "dca_executions" and row.get("attempt_type") == "maker_limit":
            if any(existing.get("dca_order_id") == row.get("dca_order_id")
                   and existing.get("trade_date_chicago") == row.get("trade_date_chicago")
                   and existing.get("attempt_type") == "maker_limit" for existing in w.rows):
                raise urllib.error.HTTPError("fake", 409, "unique violation", {},
                                              io.BytesIO(b"unique"))
        return original(table, row)

    w.sb_insert = insert
    return w


def run_pair(w, order=None):
    with w.installed():
        return kr.execute_pair(dict(order or ORDER), dict(MAKER_SETTINGS),
                               TODAY, "test-user")


def changed_time():
    return {**ORDER, "target_time": "07:45"}


def t_skip_then_funds(r):
    w = indexed_world(World())
    seed_skip(w)  # Legacy 2026-11-22-shaped row without skip_count.
    w.balance = (15.0, 0.0, "BalanceEx")
    out = run_pair(w, changed_time())
    r.check("retry places one maker order", out["status"], "limit_open")
    r.check("one AddOrder", len(w.calls("AddOrder")), 1)
    r.check("one execution row", len(w.rows), 1)
    r.check("old id kept despite time edit", w.rows[0]["cl_ord_id"], CL)
    raw = json.loads(w.rows[0]["raw"])
    r.check("legacy skip counted", raw["skip_count"], 1)
    r.check("legacy attempt preserved", len(raw["skip_history"]), 1)
    r.check("provider id unchanged", w.calls("AddOrder")[0]["cl_ord_id"], PID)


def t_skip_still_unfunded(r):
    w = indexed_world(World())
    seed_skip(w)
    w.balance = (3.9, 470.0, "BalanceEx")
    out = run_pair(w, changed_time())
    r.check("second skip recorded", out["status"], "skipped_insufficient_funds")
    r.check("no AddOrder", w.calls("AddOrder"), [])
    r.check("still one row", len(w.rows), 1)
    raw = json.loads(w.rows[0]["raw"])
    r.check("skip_count increments", raw["skip_count"], 2)
    r.check("both skip observations retained", len(raw["skip_history"]), 2)


def t_already_bought(r):
    w = indexed_world(World())
    w.add_row(id=7, cl_ord_id=CL, status="filled",
              attempt_type="maker_limit", dca_order_id=1,
              raw={"kraken_cl": PID}, order_id="OID-OLD")
    out = run_pair(w, changed_time())
    r.check("no second claim", out["status"], "already_claimed")
    r.check("no AddOrder", w.calls("AddOrder"), [])


def t_unknown_never_retries(r):
    w = indexed_world(World())
    w.add_row(id=7, cl_ord_id=CL, status="claimed",
              attempt_type="maker_limit", dca_order_id=1,
              raw={"kraken_cl": PID, "submission_state": "UNKNOWN"}, order_id=None)
    out = run_pair(w, changed_time())
    r.check("unknown remains blocked", out["status"], "already_claimed")
    r.check("no AddOrder", w.calls("AddOrder"), [])


def t_two_cron_one_claim(r):
    w = indexed_world(World())
    seed_skip(w)
    w.balance = (15.0, 0.0, "BalanceEx")
    original = w.sb_update
    other = {}

    def raced_update(table, filters, updates):
        result = original(table, filters, updates)
        if filters.get("status") == "eq.skipped_insufficient_funds" and result and not other:
            other["result"] = kr.execute_pair(changed_time(), dict(MAKER_SETTINGS),
                                              TODAY, "test-user")
        return result

    w.sb_update = raced_update
    out = run_pair(w, changed_time())
    r.check("winning run accepted", out["status"], "limit_open")
    r.check("loser blocked", other["result"]["status"], "already_claimed")
    r.check("one AddOrder between cron runs", len(w.calls("AddOrder")), 1)


def t_cap_and_unproven_fail_closed(r):
    w = indexed_world(World())
    seed_skip(w, count=4)
    r.check("four skips block another attempt",
            run_pair(w, changed_time())["status"], "already_claimed")
    r.check("no AddOrder at cap", w.calls("AddOrder"), [])
    w2 = indexed_world(World())
    seed_skip(w2)
    w2.rows[0]["raw"] = json.dumps({"kraken_cl": PID})  # no balance evidence
    r.check("incomplete evidence blocked",
            run_pair(w2, changed_time())["status"], "already_claimed")
    r.check("no AddOrder without evidence", w2.calls("AddOrder"), [])


TESTS = [
    ("skip -> funds -> one buy", t_skip_then_funds),
    ("skip -> still low -> audited skip", t_skip_still_unfunded),
    ("already bought -> blocked", t_already_bought),
    ("unknown -> blocked", t_unknown_never_retries),
    ("two cron -> one claim", t_two_cron_one_claim),
    ("cap and missing proof fail closed", t_cap_and_unproven_fail_closed),
]

if __name__ == "__main__":
    raise SystemExit(Runner("zero-spend retry").run(TESTS))
