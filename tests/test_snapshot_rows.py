"""The two mirror-snapshot row builders.

WHY THESE EXIST AT ALL. `kraken_open_orders` was created to answer "what was my
money committed to at 06:53" -- the question the 2026-07-30 refusal could not be
answered from. Its sync then ran once a day at 15:00, so the table could never
observe the moment it was built for. v1.7.4 takes the snapshot from inside the
trading run instead, which already happens at that moment.

WHY THEY ARE TESTED HERE RATHER THAN BY THE SCHEMA GATE. `check_schema_columns`
resolves dict literals passed to sb_insert. These rows now come back from a
function, which the gate cannot follow, so moving the builders out of the call
site silently dropped two payloads from its count. The keys are checked directly
against db/schema-columns.json below -- stronger than the gate, because it
proves the exact dicts that get written, not the literal that looks like them.
"""
import json
from pathlib import Path

from _harness import Runner, kr

SCHEMA = json.loads((Path(__file__).resolve().parent.parent
                     / "db" / "schema-columns.json").read_text())["tables"]

TS = "2026-08-02T06:53:00+00:00"
UID = "11111111-2222-3333-4444-555555555555"


def columns(table):
    cols = SCHEMA[table]
    return set(cols if isinstance(cols, list) else cols.get("columns", []))


# ── Balance ──────────────────────────────────────────────────────

def test_balance_accepts_both_kraken_shapes(t):
    # `Balance` returns a scalar per asset; `BalanceEx` returns a dict with
    # `balance` and `hold_trade`. The sync calls the first and the trading path
    # the second. A builder that understood only one would write an EMPTY
    # snapshot for the other and report success doing it.
    plain = kr.balance_rows({"ZUSD": "21.66", "KAS": "4852.67"}, UID, TS)
    rich = kr.balance_rows(
        {"ZUSD": {"balance": "21.66", "hold_trade": "0.0"},
         "KAS": {"balance": "4852.67", "hold_trade": "0.0"}}, UID, TS)
    t.check("plain row count", len(plain), 2)
    t.check("both shapes agree", [r["balance"] for r in plain],
            [r["balance"] for r in rich])


def test_balance_drops_zeros_and_junk(t):
    rows = kr.balance_rows(
        {"ZUSD": "0.0", "KAS": "1.5", "XETH": "0", "BAD": None, "WORSE": "abc"},
        UID, TS)
    t.check("only the non-zero, parseable asset survives",
            [r["asset"] for r in rows], ["KAS"])


def test_balance_keeps_negatives(t):
    # USDG showed a NEGATIVE balance at Kraken on 2026-07-28. Dropping it would
    # hide the one row worth looking at.
    rows = kr.balance_rows({"USDG": "-9.13992164"}, UID, TS)
    t.check("negative balance is kept", len(rows), 1)
    t.check("value preserved", rows[0]["balance"], -9.13992164)


def test_balance_empty_and_none(t):
    t.check("empty dict", kr.balance_rows({}, UID, TS), [])
    t.check("None does not raise", kr.balance_rows(None, UID, TS), [])


def test_balance_columns_exist(t):
    rows = kr.balance_rows({"ZUSD": "21.66"}, UID, TS)
    unknown = set(rows[0]) - columns("kraken_balances")
    t.check("no column that kraken_balances does not have", unknown, set())


# ── Open orders ──────────────────────────────────────────────────

RESTING = {
    "open": {
        "OABC12-DEFGH-IJKLMN": {
            "status": "open",
            "opentm": 1785585797.729765,
            "vol": "367.80499",
            "vol_exec": "0",
            "cost": "0",
            "fee": "0",
            "oflags": "post,fciq",
            "cl_ord_id": "dca-KASUSD-2026-08-02-704",
            "descr": {
                "pair": "KASUSD", "type": "buy", "ordertype": "limit",
                "price": "0.02708",
                "order": "buy 367.80499 KASUSD @ limit 0.02708",
            },
        }
    }
}


def test_open_orders_takes_the_LIMIT_price(t):
    # descr.price is the limit; the top-level `price` is the average fill and
    # reads 0 on an untouched order. Taking the wrong one values every resting
    # order at nothing -- which is exactly the blindness that made the 07-30
    # refusal unexplainable.
    order = dict(RESTING["open"]["OABC12-DEFGH-IJKLMN"], price="0")
    rows = kr.open_order_rows({"open": {"X": order}}, UID, TS)
    t.check("limit price, not average fill", rows[0]["price"], 0.02708)


def test_open_orders_shape(t):
    rows = kr.open_order_rows(RESTING, UID, TS)
    t.check("one row", len(rows), 1)
    r = rows[0]
    t.check("txid becomes the key", r["order_txid"], "OABC12-DEFGH-IJKLMN")
    t.check("client id carried", r["cl_ord_id"], "dca-KASUSD-2026-08-02-704")
    t.check("snapshot_ts is the one passed in", r["snapshot_ts"], TS)
    t.check_true("opened_at_utc parsed", r["opened_at_utc"].startswith("2026-"))
    t.check_true("raw is a JSON string", isinstance(r["raw"], str))
    t.check_true("raw round-trips", json.loads(r["raw"])["vol"] == "367.80499")


def test_open_orders_empty_shapes(t):
    t.check("no open orders", kr.open_order_rows({"open": {}}, UID, TS), [])
    t.check("missing key", kr.open_order_rows({}, UID, TS), [])
    t.check("None does not raise", kr.open_order_rows(None, UID, TS), [])


def test_open_orders_columns_exist(t):
    rows = kr.open_order_rows(RESTING, UID, TS)
    unknown = set(rows[0]) - columns("kraken_open_orders")
    t.check("no column that kraken_open_orders does not have", unknown, set())


if __name__ == "__main__":
    import sys
    sys.exit(Runner("snapshot row builders").run([
        ("balance accepts both Kraken shapes", test_balance_accepts_both_kraken_shapes),
        ("balance drops zeros and junk", test_balance_drops_zeros_and_junk),
        ("balance keeps negatives", test_balance_keeps_negatives),
        ("balance empty and None", test_balance_empty_and_none),
        ("balance columns exist", test_balance_columns_exist),
        ("open orders take the LIMIT price", test_open_orders_takes_the_LIMIT_price),
        ("open orders shape", test_open_orders_shape),
        ("open orders empty shapes", test_open_orders_empty_shapes),
        ("open orders columns exist", test_open_orders_columns_exist),
    ]))
