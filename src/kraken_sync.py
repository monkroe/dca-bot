#!/usr/bin/env python3
"""Mirror Kraken's private account state into Supabase — Balance, TradesHistory,
Ledgers.

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

READ ONLY. This process calls Balance, TradesHistory and Ledgers. It never
places, amends or cancels an order. It borrows kraken_run's request signing and
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

VERSION = "1.0.0"

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
    print("      tick ONLY: Query (Funds), Query closed orders & trades, Query ledger entries")
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
        balances = kr.kraken_private("Balance")
    except Exception as e:
        status = "permission_denied" if is_permission_error(e) else "error"
        print(f"  {kr.ICONS['FAIL']} {e}")
        write_state("balances", status=status, detail=str(e))
        return 0

    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"user_id": USER_ID, "snapshot_ts": now, "asset": asset, "balance": float(amount)}
        for asset, amount in sorted(balances.items())
        if float(amount) != 0
    ]
    if rows:
        kr.sb_insert("kraken_balances", rows)
    for r in rows:
        print(f"    {r['asset']:8s} {r['balance']}")
    write_state("balances", last_time_utc=now, status="ok", rows_seen=len(rows))
    print(f"  {kr.ICONS['OK']} {len(rows)} asset(s)")
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
    for name, fn in (("balances", sync_balances), ("trades", sync_trades),
                     ("ledgers", sync_ledgers)):
        try:
            outcomes[name] = fn()
        except Exception as e:
            print(f"  {kr.ICONS['FAIL']} {name} crashed: {e}")
            outcomes[name] = -1

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
