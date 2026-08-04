#!/usr/bin/env python3
"""Mirror Kraken's private account state into Supabase — Balance, OpenOrders,
TradesHistory, Ledgers.

WHY THIS EXISTS. Everything the system knows about the Kraken account is
inferred from what the bot itself did, so anything done by hand in the Kraken
app is invisible. `bf_holdings` only ever accumulates bot buys; a sell or a
withdrawal never reduces it. On 2026-07-28 that gap was measured once this
mirror existed: roughly 59,850 KAS was sold across eight trades between 05-26
and 06-30, plus a 9,486 KAS spend, none of it visible anywhere in this database.
Kraken is the only source of truth for the account, and this is its mirror.
(An earlier draft of this docstring said ~14,865 KAS left around 2026-03-31,
reasoning from a note in a manual export. That was wrong: March and April held
only buys. Corrected here so the code does not contradict the hub.)

READ ONLY. This process calls Balance, OpenOrders, TradesHistory and Ledgers.
It never places, amends or cancels an order. It borrows kraken_run's request signing and
Supabase helpers. Importing that module is NOT free: it reads KRAKEN_API_KEY,
KRAKEN_API_SECRET, SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY with os.environ[...]
at module level and raises KeyError if any is missing, so this job cannot start
without credentials present under those names (which is how the workflow maps
the read-only secret). An earlier version of this docstring claimed the import
was side-effect free; it is not. This job still deliberately does NOT share the
trading path, and it runs from its own workflow so that a sync failure
can never interfere with a buy.

CREDENTIALS. This runs on its OWN Kraken key, with query permissions only and no
trade or withdraw rights. The trading key is deliberately absent from the sync
job's environment, so credentials that leak from here cannot place an order.
Two further reasons beyond the obvious one:
  * permissions here only ever need to GROW (Ledgers, Trade History), and
    growing them on the trading key would widen what the trading key can do;
  * Kraken requires a strictly increasing nonce PER KEY. The trading bot runs
    every five minutes and this sync would overlap it sooner or later; sharing a
    key makes "Invalid nonce" a question of timing. Separate keys remove that
    failure mode entirely rather than making it rare.
The shared client reads its credentials from module-level names in kraken_run,
so the workflow simply maps the read-only secret onto those names -- no second
signing implementation, and nothing to keep in sync between two copies.

PERMISSIONS. Balance already works (the buy preflight has used it since Phase 1).
OpenOrders does NOT: this file first claimed it "needs nothing beyond Balance,
the trading path has called that endpoint since Phase 2", and the first live run
returned permission denied. The trading key can call it; the read-only key this
job runs on cannot, because "Query open orders & trades" is a SEPARATE tick from
"Query closed orders & trades" and was never enabled. Two keys, two permission
sets -- reasoning from what the trading key can do said nothing about this one.
TradesHistory and Ledgers additionally need the key's "Query Closed Orders & Trades"
and "Query Ledger Entries" permissions, which cannot be checked from here. Each source is therefore
attempted independently and a permission error is recorded against that source
alone, so one blocked endpoint still leaves the others synced and the first run
tells us exactly which is missing.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import kraken_run as kr

VERSION = "1.2.3"

# What THIS run fetched, handed to `maybe_warn_low_balance` after the source
# loop. Not read from the mirror tables on purpose: those hold every snapshot
# ever taken, and a warning built on the newest row cannot tell "written four
# seconds ago" from "written before the endpoint started failing". None means
# the source did not run or was denied, which is a different message from zero.
_LAST_BALANCES = None
_LAST_OPEN_ORDER_ROWS = None

# Kraken pages these 50 at a time and exposes the total as `count`.
PAGE = 50
# Private calls decay from a small counter; TradesHistory and Ledgers cost more
# than a plain query, so pages are spaced rather than fired back to back.
PAGE_PAUSE_SECONDS = 2.0
# Each run rewinds its watermark by this much. A trade landing in the same
# second as the previous cutoff would otherwise sit exactly on the boundary and
# could be missed by both windows; the overlap costs nothing because rows are
# upserted on Kraken's own ids.
WATERMARK_REWIND_MINUTES = 30
# Guard against an unbounded loop if `count` and the returned pages disagree.
MAX_PAGES = 200

def check_credentials() -> bool:
    """Fail loudly and specifically rather than letting an empty key produce a
    signature error that reads like a Kraken outage."""
    if kr.KRAKEN_API_KEY and kr.KRAKEN_API_SECRET:
        return True
    print(f"{kr.ICONS['FAIL']} No Kraken credentials in this job's environment.")
    print("   This sync expects its OWN read-only key, not the trading one:")
    print("   1. Kraken -> Settings -> API -> Add key")
    print("      tick ONLY: Query (Funds), Query open orders & trades,")
    print("                 Query closed orders & trades, Query ledger entries")
    print("      leave OFF: Create & modify orders, Cancel & close orders, Withdraw, Deposit, Earn")
    print("   2. GitHub -> repo Settings -> Secrets -> Actions, add")
    print("      KRAKEN_RO_API_KEY and KRAKEN_RO_API_SECRET")
    print("   The trading key is intentionally not passed to this workflow.")
    return False


def load_user_id() -> str | None:
    """Same source the trading path uses -- dca_settings.id=1 -- rather than a
    new secret. Missing is survivable here: these tables are a mirror of one
    account, so a NULL user_id costs nothing that a failed sync would not cost
    more."""
    try:
        rows = kr.sb_get("dca_settings", {"id": "eq.1"})
        return (rows or [{}])[0].get("user_id")
    except Exception as e:
        print(f"  {kr.ICONS['WARN']} user_id lookup failed: {e}")
        return None


USER_ID: str | None = None


# ═══════════════════════════════════════════════════════════════
#  SUPABASE — upsert needs a header sb_request does not set
# ═══════════════════════════════════════════════════════════════

def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    """PostgREST upsert. Separate from kraken_run.sb_insert because that one
    sends `Prefer: return=representation` only; merging duplicates needs
    `resolution=merge-duplicates`, and re-runs MUST be free."""
    if not rows:
        return
    url = (f"{kr.SUPABASE_URL}/rest/v1/{table}"
           f"?on_conflict={urllib.parse.quote(on_conflict)}")
    req = urllib.request.Request(
        url,
        data=json.dumps(rows).encode(),
        headers={
            "apikey": kr.SUPABASE_KEY,
            "Authorization": f"Bearer {kr.SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def read_state(source: str) -> dict:
    try:
        rows = kr.sb_get("kraken_sync_state", {"source": f"eq.{source}"})
        return (rows or [{}])[0]
    except Exception as e:
        print(f"  {kr.ICONS['WARN']} state read failed for {source}: {e}")
        return {}


def write_state(source: str, *, last_time_utc=None, status: str, detail: str = "",
                rows_seen: int = 0) -> None:
    row = {
        "source": source,
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "last_status": status,
        "detail": detail[:500],
        "rows_seen": rows_seen,
    }
    if last_time_utc is not None:
        row["last_time_utc"] = last_time_utc
    try:
        sb_upsert("kraken_sync_state", [row], "source")
    except Exception as e:
        print(f"  {kr.ICONS['WARN']} state write failed for {source}: {e}")


def since_epoch(source: str) -> float | None:
    """Watermark, rewound. None means "never synced — fetch everything"."""
    st = read_state(source)
    raw = st.get("last_time_utc")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt - timedelta(minutes=WATERMARK_REWIND_MINUTES)).timestamp()


def ts(value) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def is_permission_error(e: Exception) -> bool:
    return "permission denied" in str(e).lower()


# ═══════════════════════════════════════════════════════════════
#  SOURCES
# ═══════════════════════════════════════════════════════════════

def sync_balances() -> int:
    """Snapshot every non-zero asset. Append-only: drift against bf_holdings is
    only visible if history is kept, and a single overwritten row hides it."""
    print(f"\n{kr.ICONS['CHART']} Balance")
    try:
        balances = kr.snapshot_balances()
    except Exception as e:
        status = "permission_denied" if is_permission_error(e) else "error"
        print(f"  {kr.ICONS['FAIL']} {e}")
        write_state("balances", status=status, detail=str(e))
        return 0

    now = datetime.now(timezone.utc).isoformat()
    # Row shape lives in kraken_run so the trading path and this one cannot
    # drift apart: both write the same table, and two builders for one table is
    # two chances to disagree about it.
    rows = kr.balance_rows(balances, USER_ID, now)
    if rows:
        kr.sb_insert("kraken_balances", rows)
    for r in rows:
        # The held amount is printed only when it is KNOWN. A blank column and a
        # "0" mean different things here, and the run log is where that is first
        # read.
        held = "" if r.get("hold_trade") is None else f"  (held {r['hold_trade']})"
        print(f"    {r['asset']:8s} {r['balance']}{held}")
    write_state("balances", last_time_utc=now, status="ok", rows_seen=len(rows))
    print(f"  {kr.ICONS['OK']} {len(rows)} asset(s)")
    # Handed to the warning, which now runs AFTER the whole source loop --
    # see `maybe_warn_low_balance`.
    global _LAST_BALANCES
    _LAST_BALANCES = balances
    return len(rows)


def maybe_warn_low_balance() -> None:
    """The ONE low-balance warning, sent from the EVENING run only.

    WHY HERE AND NOT IN THE TRADING RUN. The warning used to fire in the buy
    preflight, four minutes before the purchase, reporting the balance about to
    be spent. Useless twice over: the number was wrong by one buy, and four
    minutes is not time to move money.

    WHY THE EVENING AND NOT MIDDAY. Measured over July's 23 shift payouts: 89%
    of the money by value arrives after 16:00. A midday warning asks Roberto to
    top up out of income he has not earned yet. The evening run lands after the
    day's payouts and roughly nine hours before the next buy window -- the last
    point at which a transfer can still change tomorrow's outcome.

    THE HOUR GUARD IS THE WHOLE DEDUPLICATION. `kraken_sync` runs three times a
    day; only the late one warns, so there is no day key to keep and no way for
    three callers to send three copies of the same sentence. Written as an hour
    range rather than an exact hour because the pg_cron schedule is UTC and
    drifts an hour with DST -- 21:00 CST and 22:00 CDT both satisfy it.

    WHY IT MOVED OUT OF `sync_balances`. The message now names the ORDERS
    holding the money, and open orders are synced after balances -- reading
    them from inside the balance step would have shown the PREVIOUS run's
    orders, up to nine hours stale. Same reason `notify_manual_fills` sits
    after the loop: a message about what the mirror learned cannot be sent
    before the mirror has learned it.

    A blocked OpenOrders key degrades to the held AMOUNT without the reason,
    which is honest; `hold_trade` comes from Balance and is a separate
    permission.

    NEVER RAISES. A warning is telemetry; the mirror must not fail because of it.
    """
    try:
        # kr.CHICAGO_TZ, not a second ZoneInfo built here: one definition of the
        # trading timezone, in the module that owns the trading day.
        hour_ct = datetime.now(timezone.utc).astimezone(kr.CHICAGO_TZ).hour
        if hour_ct < 20:
            return
        if _LAST_BALANCES is None:
            # Balance sync failed or was denied. Warning on the last snapshot in
            # the table would put a stale number in the one message that exists
            # to be acted on tonight.
            print(f"  {kr.ICONS['WARN']} low-balance check skipped: no fresh balances")
            return
        row = (_LAST_BALANCES or {}).get("ZUSD")
        if isinstance(row, dict):
            total = float(row.get("balance") or 0)
            held = float(row.get("hold_trade") or 0)
        else:
            total, held = float(row or 0), 0.0
        spendable = max(total - held, 0.0)

        orders = kr.sb_get("dca_orders", {"enabled": "eq.true",
                                          "select": "base_quote_amount,bonus_quote_amount"})
        burn = sum(float(o.get("base_quote_amount") or 0)
                   + float(o.get("bonus_quote_amount") or 0) for o in (orders or []))
        settings = (kr.sb_get("dca_settings", {"select": "low_balance_warn_days"}) or [{}])[0]
        # attach_distances adds how far each resting order sits from the market.
        # Without it the warning still names the order and simply omits the
        # figure -- the same degradation as a failed ticker read, so a change
        # here can never turn a missing percentage into a wrong one.
        kr.warn_if_low_balance(spendable, burn, settings, total=total, held=held,
                               holds=kr.attach_distances(kr.usd_holds(_LAST_OPEN_ORDER_ROWS)))
    except Exception as e:
        print(f"  {kr.ICONS['WARN']} low-balance check skipped: {e}")


def sync_open_orders() -> int:
    """What the money is SPOKEN FOR. The other three sources describe money
    that has already moved; an open order is money that has not moved and
    cannot be spent, and until 2026-07-30 that state had no record anywhere.
    That morning a buy was refused for insufficient funds while the balance
    looked sufficient, and nothing in this database could say what the funds
    were committed to.

    Append-only, like balances: the useful question is "what was resting at
    06:53", and a table that overwrites itself can only answer "what is
    resting now". Orders that fill or cancel simply stop appearing.

    No pagination -- OpenOrders returns the whole set, unlike the history
    endpoints.
    """
    print(f"\n{kr.ICONS['CHART']} OpenOrders")
    try:
        result = kr.kraken_private("OpenOrders")
    except Exception as e:
        status = "permission_denied" if is_permission_error(e) else "error"
        print(f"  {kr.ICONS['FAIL']} {e}")
        if status == "permission_denied":
            print("    → the read-only key lacks 'Query open orders & trades'.")
            print("      It is a SEPARATE tick from 'Query closed orders & trades';")
            print("      having the latter says nothing about the former.")
        write_state("open_orders", status=status, detail=str(e))
        return 0

    now = datetime.now(timezone.utc).isoformat()
    # Same reason as sync_balances: one builder, in kraken_run, shared with the
    # trading path. `descr.price` vs the top-level `price` is the trap it
    # encodes -- the latter is the average fill and reads 0 on an untouched
    # order, which would value every resting order at nothing.
    rows = kr.open_order_rows(result, USER_ID, now)

    if rows:
        sb_upsert("kraken_open_orders", rows, "snapshot_ts,order_txid")
    # Handed to the low-balance warning so it can say WHICH order holds the
    # money, not just how much. Set even when empty -- "no resting orders" is
    # an answer, and it is not the same as "could not ask".
    global _LAST_OPEN_ORDER_ROWS
    _LAST_OPEN_ORDER_ROWS = rows
    for r in rows:
        locked = (r["vol"] - r["vol_exec"]) * r["price"]
        print(f"    {r['pair']:10s} {r['side']:4s} {r['ordertype']:8s}"
              f" {r['vol']} @ {r['price']}  locked ~{locked:.2f}")
    write_state("open_orders", last_time_utc=now, status="ok", rows_seen=len(rows))
    print(f"  {kr.ICONS['OK']} {len(rows)} resting order(s)")
    return len(rows)


def _paged(endpoint: str, key: str, start: float | None):
    """Walk Kraken's `ofs` pagination, yielding one id->record dict per page."""
    offset = 0
    for _ in range(MAX_PAGES):
        params = {"ofs": offset}
        if start is not None:
            params["start"] = int(start)
        result = kr.kraken_private(endpoint, params)
        block = result.get(key) or {}
        if not block:
            return
        yield block
        offset += len(block)
        total = int(result.get("count") or 0)
        if offset >= total:
            return
        time.sleep(PAGE_PAUSE_SECONDS)
    print(f"  {kr.ICONS['WARN']} {endpoint}: hit MAX_PAGES, stopping early")


def sync_trades() -> int:
    """Every trade, including ones made by hand in the app — the first time
    sells become visible anywhere in this database."""
    print(f"\n{kr.ICONS['CHART']} TradesHistory")
    start = since_epoch("trades")
    print("  from: " + (ts(start) if start else "the beginning"))
    seen, newest = 0, None
    try:
        for block in _paged("TradesHistory", "trades", start):
            rows = []
            for trade_id, t in block.items():
                when = float(t.get("time") or 0)
                newest = when if newest is None else max(newest, when)
                rows.append({
                    "trade_id": trade_id,
                    "user_id": USER_ID,
                    "order_txid": t.get("ordertxid"),
                    "pair": t.get("pair"),
                    "time_utc": ts(when),
                    "side": t.get("type"),
                    "ordertype": t.get("ordertype"),
                    "price": float(t.get("price") or 0),
                    "cost": float(t.get("cost") or 0),
                    "fee": float(t.get("fee") or 0),
                    "vol": float(t.get("vol") or 0),
                    "margin": float(t.get("margin") or 0),
                    "raw": json.dumps(t),
                })
            sb_upsert("kraken_trades", rows, "trade_id")
            seen += len(rows)
            print(f"    +{len(rows)} (total {seen})")
    except Exception as e:
        status = "permission_denied" if is_permission_error(e) else "error"
        print(f"  {kr.ICONS['FAIL']} {e}")
        if status == "permission_denied":
            print("    → the API key lacks 'Query closed orders & trades' + 'Query ledger entries'")
        write_state("trades", status=status, detail=str(e), rows_seen=seen)
        return seen

    write_state("trades", last_time_utc=ts(newest) if newest else None,
                status="ok", rows_seen=seen)
    print(f"  {kr.ICONS['OK']} {seen} trade(s)")
    return seen


def notify_manual_fills() -> None:
    """Tell Roberto when an order HE placed by hand has filled.

    THE GAP THIS CLOSES. On 2026-08-03 a limit order for the whole USDT balance
    filled at 02:44. He found out from a conversation at 07:30, because nothing
    in the system says anything about an order it did not place. The DCA
    announces its own fills; a manual order announced nothing.

    ITS OWN WATERMARK, deliberately not the `trades` one. That watermark is
    rewound by WATERMARK_REWIND_MINUTES on every read, so trades already seen
    come back on purpose -- correct for an upsert, and it would mean sending the
    same message again on every run. `trade_notify` moves forward only.

    THE FIRST RUN SENDS NOTHING. With no watermark it would announce every trade
    in the account's history, which is how a new notification trains its reader
    to mute it. It records the current position and stays quiet.

    DCA FILLS ARE EXCLUDED by `order_txid`, checked against both execution
    tables. The DCA already sends its own message and does it better -- with
    impact, all-in bps and the OHLC bands, none of which are known here.

    LAG IS REAL AND IS NOT HIDDEN: the sync runs at 08:00, 13:00 and 22:00, so
    a fill can wait up to ten hours. That is the cost of not polling Kraken more
    often, and it was chosen. The message therefore leads with the fill time,
    not with the word "now".

    NEVER RAISES.
    """
    try:
        st = read_state("trade_notify")
        last = st.get("last_time_utc")
        if not last:
            newest = sb_get_max_trade_time()
            write_state("trade_notify", last_time_utc=newest or ts(time.time()),
                        status="ok", detail="initialised, nothing sent")
            print(f"  {kr.ICONS['OK']} trade_notify initialised (no messages on first run)")
            return

        rows = kr.sb_get("kraken_trades", {
            "time_utc": f"gt.{last}",
            "select": "trade_id,order_txid,pair,side,price,cost,fee,vol,time_utc",
            "order": "time_utc.asc",
        }) or []
        if not rows:
            write_state("trade_notify", status="ok", rows_seen=0)
            return

        txids = sorted({r.get("order_txid") for r in rows if r.get("order_txid")})
        dca_txids: set[str] = set()
        if txids:
            in_list = "(" + ",".join(txids) + ")"
            for table in ("dca_executions", "strike_dca_executions"):
                got = kr.sb_get(table, {"order_id": f"in.{in_list}",
                                        "select": "order_id"}) or []
                dca_txids |= {g["order_id"] for g in got if g.get("order_id")}

        manual = [r for r in rows if r.get("order_txid") not in dca_txids]
        newest_ts = max(r["time_utc"] for r in rows)

        if not manual:
            write_state("trade_notify", last_time_utc=newest_ts, status="ok", rows_seen=0)
            print(f"  {kr.ICONS['OK']} {len(rows)} new trade(s), all DCA — nothing to announce")
            return

        # Grouped by order: a partly filled limit order arrives as several
        # trades, and three messages about one decision is noise.
        by_order: dict[str, dict] = {}
        for r in manual:
            k = r.get("order_txid") or r["trade_id"]
            g = by_order.setdefault(k, {"pair": r.get("pair"), "side": r.get("side"),
                                        "vol": 0.0, "cost": 0.0, "fee": 0.0,
                                        "when": r["time_utc"], "n": 0, "tids": []})
            g["vol"] += float(r.get("vol") or 0)
            g["cost"] += float(r.get("cost") or 0)
            g["fee"] += float(r.get("fee") or 0)
            g["when"] = max(g["when"], r["time_utc"])
            g["n"] += 1
            g["tids"].append(r["trade_id"])

        # WHICH CURRENCY THE FEE WAS ACTUALLY TAKEN IN, from the ledger.
        # `kraken_trades.fee` is always stated in the quote currency, which is a
        # conversion, not the event. On 2026-08-03 a KASUSDT buy was charged
        # 47.57577 KAS -- the reason the balance rose by less than the order
        # volume, and a number that appears nowhere in the trades table. The
        # ledger row carrying a non-zero fee names the asset it left in.
        fee_assets = _fee_assets_by_trade([t for g in by_order.values() for t in g["tids"]])

        for order_txid, g in by_order.items():
            avg = g["cost"] / g["vol"] if g["vol"] else 0.0
            native: dict[str, float] = {}
            for tid in g["tids"]:
                for asset, amt in fee_assets.get(tid, {}).items():
                    native[asset] = native.get(asset, 0.0) + amt
            # Printed only when it says something the dollar figure does not:
            # a fee in the quote currency is already what `Fee` shows.
            quote = str(g["pair"] or "")
            native_txt = "".join(
                f"  ({amt:.5f} {asset})"
                for asset, amt in native.items()
                if amt > 0 and not quote.endswith(asset)
            )
            when_ct = datetime.fromisoformat(str(g["when"]).replace("Z", "+00:00")) \
                .astimezone(kr.CHICAGO_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
            verb = "BUY" if str(g["side"]).lower() == "buy" else "SELL"
            partial = f"\nParts:  {g['n']}" if g["n"] > 1 else ""
            kr.tg_send(kr.msg_ok(
                f"Manual {verb} filled: {g['pair']}",
                f"{when_ct}\n\n"
                f"Amount: {g['vol']:.8f}\n"
                f"Price:  ${avg:.6f}\n"
                f"Cost:   ${g['cost']:.4f}\n"
                f"Fee:    ${g['fee']:.4f}{native_txt}"
                f"{partial}\n\n"
                f"Order:  {order_txid}"
            ))
            print(f"  {kr.ICONS['OK']} announced manual fill {order_txid}")

        write_state("trade_notify", last_time_utc=newest_ts, status="ok",
                    rows_seen=len(by_order))
    except Exception as e:
        print(f"  {kr.ICONS['WARN']} manual-fill notify skipped: {e}")
        try:
            write_state("trade_notify", status="error", detail=str(e))
        except Exception:
            pass


def _fee_assets_by_trade(trade_ids: list[str]) -> dict[str, dict[str, float]]:
    """{trade_id: {asset: fee}} from `kraken_ledgers`, fee-bearing rows only.

    The ledger's `refid` is the TRADE id, not the order id -- checked against
    2026-08-03, where one order produced two ledger rows sharing one refid, and
    only the KAS side carried the fee.

    Returns {} on any failure: the fill message is worth sending without this
    detail, and a missing parenthesis must not cost the notification.
    """
    if not trade_ids:
        return {}
    try:
        rows = kr.sb_get("kraken_ledgers", {
            "refid": "in.(" + ",".join(sorted(set(trade_ids))) + ")",
            "select": "refid,asset,fee",
        }) or []
    except Exception as e:
        print(f"  {kr.ICONS['WARN']} fee-asset lookup skipped: {e}")
        return {}
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        fee = float(r.get("fee") or 0)
        if fee <= 0:
            continue
        out.setdefault(r["refid"], {})[r.get("asset") or "?"] = fee
    return out


def sb_get_max_trade_time() -> str | None:
    """Newest trade already in the mirror, for initialising the notify watermark."""
    rows = kr.sb_get("kraken_trades", {"select": "time_utc", "order": "time_utc.desc",
                                       "limit": "1"}) or []
    return rows[0]["time_utc"] if rows else None


def sync_ledgers() -> int:
    """Every movement. Trades alone cannot answer where an asset went: a
    withdrawal to a cold wallet is not a trade, and the disposal this whole
    exercise is chasing may be either."""
    print(f"\n{kr.ICONS['CHART']} Ledgers")
    start = since_epoch("ledgers")
    print("  from: " + (ts(start) if start else "the beginning"))
    seen, newest = 0, None
    try:
        for block in _paged("Ledgers", "ledger", start):
            rows = []
            for ledger_id, l in block.items():
                when = float(l.get("time") or 0)
                newest = when if newest is None else max(newest, when)
                rows.append({
                    "ledger_id": ledger_id,
                    "user_id": USER_ID,
                    "refid": l.get("refid"),
                    "time_utc": ts(when),
                    "type": l.get("type"),
                    "subtype": l.get("subtype"),
                    "asset": l.get("asset"),
                    "amount": float(l.get("amount") or 0),
                    "fee": float(l.get("fee") or 0),
                    "balance": float(l.get("balance") or 0),
                    "raw": json.dumps(l),
                })
            sb_upsert("kraken_ledgers", rows, "ledger_id")
            seen += len(rows)
            print(f"    +{len(rows)} (total {seen})")
    except Exception as e:
        status = "permission_denied" if is_permission_error(e) else "error"
        print(f"  {kr.ICONS['FAIL']} {e}")
        if status == "permission_denied":
            print("    → the API key lacks 'Query closed orders & trades' + 'Query ledger entries'")
        write_state("ledgers", status=status, detail=str(e), rows_seen=seen)
        return seen

    write_state("ledgers", last_time_utc=ts(newest) if newest else None,
                status="ok", rows_seen=seen)
    print(f"  {kr.ICONS['OK']} {seen} ledger entr(ies)")
    return seen


# ═══════════════════════════════════════════════════════════════

def main() -> int:
    global USER_ID
    print(f"{kr.ICONS['BOT']} Kraken sync v{VERSION} (read-only)")
    print(f"{kr.ICONS['CLOCK']} "
          f"{datetime.now(timezone.utc).astimezone(kr.CHICAGO_TZ):%Y-%m-%d %H:%M:%S %Z}")
    if not check_credentials():
        return 1
    USER_ID = load_user_id()
    print(f"   user_id: {USER_ID[:8] + '...' if USER_ID else '(none)'}")
    print(f"   key:     ...{kr.KRAKEN_API_KEY[-4:]} (expected: query-only)")

    # Independent on purpose: one blocked endpoint must not hide the others.
    outcomes = {}
    for name, fn in (("balances", sync_balances), ("open_orders", sync_open_orders),
                     ("trades", sync_trades), ("ledgers", sync_ledgers)):
        try:
            outcomes[name] = fn()
        except Exception as e:
            print(f"  {kr.ICONS['FAIL']} {name} crashed: {e}")
            outcomes[name] = -1

    # AFTER the sources, never inside the loop: these announce what the mirror
    # just learned, so they must not run before it has been written, and a
    # failure here must not colour the sync's own outcome.
    notify_manual_fills()
    # Needs BOTH balances (how much is held) and open orders (what is holding
    # it), so it cannot live inside either one of them.
    maybe_warn_low_balance()

    # Success is judged on the RECORDED STATUS, not the row count. A denied
    # source catches its own error and returns 0 rows, which is
    # indistinguishable from a source that legitimately had nothing new -- so
    # counting rows would report a fully blocked run as a success.
    statuses = {name: (read_state(name).get("last_status") or "unknown")
                for name in outcomes}

    print(f"\n{'=' * 50}")
    for name, n in outcomes.items():
        rows = "crashed" if n < 0 else f"{n} row(s)"
        print(f"  {name:10s} {statuses[name]:18s} {rows}")

    blocked = [n for n, st in statuses.items() if st == "permission_denied"]
    if blocked:
        print(f"\n{kr.ICONS['WARN']} blocked by key permissions: {', '.join(blocked)}")
        print("   Kraken → Settings → API → edit the key → tick"
              " 'Query closed orders & trades' + 'Query ledger entries'. Nothing else needs to change.")

    # Exit non-zero only if NO source succeeded -- a partial sync is still
    # progress, and a red workflow for one known-missing permission is noise
    # that trains you to ignore the red.
    return 0 if any(st == "ok" for st in statuses.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
