"""In-memory Supabase + Kraken for the ROB-21 execution-path tests.

No network and no credentials: every external function the buy path calls is
replaced for the duration of a `with world.installed():` block.

Two properties are enforced here for EVERY test that uses it, so no individual
test has to remember them:

  * no outgoing Kraken request may carry the legacy client-id field name;
  * the fake Kraken honours the `cl_ord_id` filter like the real one, unless a
    test switches that off on purpose (`ignore_filter`) to prove a foreign
    order is rejected.

Rows keep `raw` as JSON TEXT, like production, where historical rows hold a
JSON string inside a jsonb column.
"""
import contextlib
import copy
import io
import json
import urllib.error

from _harness import kr

LEGACY_FIELD = "cl_" + "ordid"   # spelled apart so the source scan stays literal-free


class World:
    def __init__(self):
        self.rows = []              # dca_executions rows
        self.events = []            # ordered ("db", kind, detail) / ("kraken", endpoint)
        self.kraken_calls = []      # (endpoint, params)
        self.tg = []
        self.finalized = []         # (cl_ord_id, txid)
        self.open_orders = {}       # txid -> order dict
        self.closed_orders = {}     # txid -> order dict
        self.endpoint_errors = {}   # endpoint -> Exception to raise
        self.ignore_filter = False  # fake Kraken ignores cl_ord_id filters
        self.register_orders = True # a successful AddOrder creates an open order
        self.add_order = lambda params: {"txid": ["OTXID-NEW"]}
        self.ticker = {"bid": 0.03418, "ask": 0.03422, "mid": 0.0342}
        self.pair_info = {"pair_decimals": 5, "lot_decimals": 5, "ordermin": 1.0}
        self.balance = (100.0, 0.0, "fake")
        self.clock_window = None    # (start, end) override for _window_bounds_for

    # ── helpers ──────────────────────────────────────────────
    def add_row(self, **row):
        row.setdefault("user_id", "test-user")
        row.setdefault("pair", "KASUSD")
        row.setdefault("trade_date_chicago", "2026-10-06")
        raw = row.get("raw")
        if raw is not None and not isinstance(raw, str):
            row["raw"] = json.dumps(raw)
        self.rows.append(row)
        return row

    def row(self, cl_ord_id):
        return next(r for r in self.rows if r["cl_ord_id"] == cl_ord_id)

    def raw(self, cl_ord_id):
        return json.loads(self.row(cl_ord_id)["raw"])

    def calls(self, endpoint):
        return [p for e, p in self.kraken_calls if e == endpoint]

    # ── fake Supabase ────────────────────────────────────────
    @staticmethod
    def _matches(row, params):
        for key, cond in params.items():
            if key in ("select", "order", "limit") or not isinstance(cond, str):
                continue
            value = row.get(key)
            if cond.startswith("eq."):
                if str(value) != cond[3:]:
                    return False
            elif cond.startswith("in.("):
                if str(value) not in cond[4:-1].split(","):
                    return False
            elif cond.startswith("lt."):
                if value is None or str(value) >= cond[3:]:
                    return False
            elif cond == "is.null":
                if value is not None:
                    return False
        return True

    def sb_get(self, table, params=None):
        if table != "dca_executions":
            return []
        return [copy.deepcopy(r) for r in self.rows if self._matches(r, params or {})]

    def sb_insert(self, table, row):
        if table != "dca_executions":
            return [dict(row)]
        if any(r["cl_ord_id"] == row["cl_ord_id"] for r in self.rows):
            raise urllib.error.HTTPError("x", 409, "conflict", {}, io.BytesIO(b"{}"))
        self.events.append(("db", "insert", row["cl_ord_id"]))
        stored = copy.deepcopy(row)
        stored.setdefault("order_id", None)
        stored.setdefault("reason", None)
        self.rows.append(stored)
        return [copy.deepcopy(stored)]

    def sb_update(self, table, filters, updates):
        if table != "dca_executions":
            return []
        hit = [r for r in self.rows if self._matches(r, filters)]
        for r in hit:
            r.update(copy.deepcopy(updates))
            self.events.append(("db", "update", sorted(updates)))
        return [copy.deepcopy(r) for r in hit]

    # ── fake Kraken ──────────────────────────────────────────
    def kraken_private(self, endpoint, params=None):
        params = copy.deepcopy(params or {})
        self.kraken_calls.append((endpoint, params))
        self.events.append(("kraken", endpoint))
        if LEGACY_FIELD in params:
            raise AssertionError(f"{endpoint} sent the legacy client-id field: {params}")
        if endpoint in self.endpoint_errors:
            raise self.endpoint_errors[endpoint]
        if endpoint == "AddOrder":
            result = self.add_order(params)
            txids = result.get("txid") if isinstance(result, dict) else None
            if self.register_orders and txids:
                self.open_orders[txids[0]] = {
                    "status": "open", "vol_exec": "0.0", "cl_ord_id": params.get("cl_ord_id"),
                }
            return result
        if endpoint in ("OpenOrders", "ClosedOrders"):
            pool = self.open_orders if endpoint == "OpenOrders" else self.closed_orders
            wanted = params.get("cl_ord_id")
            picked = {
                tx: o for tx, o in pool.items()
                if self.ignore_filter or wanted is None or o.get("cl_ord_id") == wanted
            }
            return {"open": picked} if endpoint == "OpenOrders" else {"closed": picked}
        if endpoint == "QueryOrders":
            tx = params.get("txid")
            order = self.open_orders.get(tx) or self.closed_orders.get(tx)
            return {tx: order} if order else {}
        if endpoint == "CancelOrder":
            return {"count": 1}
        raise AssertionError(f"unexpected Kraken endpoint: {endpoint}")

    # ── install ──────────────────────────────────────────────
    @contextlib.contextmanager
    def installed(self):
        import datetime as _dt

        def window(_date, _time, _minutes):
            if self.clock_window:
                return self.clock_window
            now = _dt.datetime.now(kr.CHICAGO_TZ)
            return now - _dt.timedelta(minutes=10), now + _dt.timedelta(minutes=50)

        replacements = {
            "sb_get": self.sb_get,
            "sb_insert": self.sb_insert,
            "sb_update": self.sb_update,
            "kraken_private": self.kraken_private,
            "tg_send": lambda text, *a, **k: self.tg.append(text),
            "snapshot_mirror": lambda *_a, **_k: None,
            "check_balance_usd": lambda: self.balance,
            "get_asset_pair_info": lambda _pair: dict(self.pair_info),
            "get_ticker_snapshot": lambda _pair: dict(self.ticker),
            "build_daily_metrics": lambda *_a, **_k: {},
            "get_cap_context": lambda *_a, **_k: (None, None, "H7"),
            "_open_orders_digest": lambda: [],
            "_window_bounds_for": window,
            "save_mid_snapshot": lambda *_a, **_k: None,
            "finalize_order": lambda cl, txid, **_k: self.finalized.append((cl, txid)),
        }
        originals = {name: getattr(kr, name) for name in replacements}
        real_sleep = kr.time.sleep
        for name, fn in replacements.items():
            setattr(kr, name, fn)
        kr.time.sleep = lambda _s: None
        try:
            yield self
        finally:
            kr.time.sleep = real_sleep
            for name, fn in originals.items():
                setattr(kr, name, fn)


MAKER_SETTINGS = {
    "dry_run": False, "order_strategy": "maker_first",
    "maker_fee_rate": 0.004, "taker_fee_rate": 0.008,
    "time_window_minutes": 60, "target_time": "7:04", "repeg_enabled": False,
}
MARKET_SETTINGS = {**MAKER_SETTINGS, "order_strategy": "market"}
ORDER = {"id": 1, "pair": "KASUSD", "base_quote_amount": 10.0, "target_time": "7:04",
         "time_window_minutes": 60}
TODAY = "2026-10-06"
CL = "dca-KASUSD-2026-10-06-704"      # what execute_pair derives for ORDER on TODAY
