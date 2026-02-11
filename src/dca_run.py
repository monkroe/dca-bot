#!/usr/bin/env python3
"""
Roberto's DCA Bot v1 — Execution Engine
GitHub Actions → Python → Kraken API → Supabase → Telegram

Usage (via GH Actions, not directly):
  python dca_run.py              # Main scheduled run
  python dca_run.py --reconcile  # Reconciliation only
  python dca_run.py --weekly     # Weekly summary only
"""

import hashlib
import hmac
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ═══════════════════════════════════════════════════════════════
#  CONFIG FROM ENVIRONMENT (GH Secrets)
# ═══════════════════════════════════════════════════════════════

KRAKEN_API_KEY    = os.environ["KRAKEN_API_KEY"]
KRAKEN_API_SECRET = os.environ["KRAKEN_API_SECRET"]
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TG_BOT_TOKEN      = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "")

CHICAGO_TZ = ZoneInfo("America/Chicago")


# ═══════════════════════════════════════════════════════════════
#  SUPABASE CLIENT (lightweight, no SDK needed)
# ═══════════════════════════════════════════════════════════════

def sb_request(method: str, path: str, body=None, params: dict | None = None):
    """Make authenticated Supabase REST API request."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else None


def sb_get(table: str, params: dict | None = None):
    return sb_request("GET", table, params=params)


def sb_insert(table: str, row: dict):
    """Insert row, return inserted row. Raises on conflict."""
    return sb_request("POST", table, body=row)


def sb_upsert(table: str, row: dict):
    """Upsert row (for claim phase — conflict = already claimed)."""
    headers_extra = {"Prefer": "return=representation,resolution=merge-duplicates"}
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    data = json.dumps(row).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,resolution=merge-duplicates",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def sb_update(table: str, match_params: dict, updates: dict):
    """Update rows matching filter."""
    url = f"{SUPABASE_URL}/rest/v1/{table}?{urllib.parse.urlencode(match_params)}"
    data = json.dumps(updates).encode()
    req = urllib.request.Request(url, data=data, method="PATCH", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


# ═══════════════════════════════════════════════════════════════
#  KRAKEN API (zero dependencies)
# ═══════════════════════════════════════════════════════════════

KRAKEN_BASE = "https://api.kraken.com"


def kraken_signature(urlpath: str, data: dict) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(KRAKEN_API_SECRET), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def kraken_private(endpoint: str, params: dict | None = None) -> dict:
    """Authenticated Kraken API call."""
    if params is None:
        params = {}
    urlpath = f"/0/private/{endpoint}"
    params["nonce"] = str(int(time.time() * 1000))

    req = urllib.request.Request(
        KRAKEN_BASE + urlpath,
        data=urllib.parse.urlencode(params).encode(),
        headers={
            "API-Key": KRAKEN_API_KEY,
            "API-Sign": kraken_signature(urlpath, params),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())

    if result.get("error"):
        raise KrakenError(result["error"])
    return result["result"]


def kraken_public(endpoint: str, params: dict | None = None) -> dict:
    """Public Kraken API call (no auth)."""
    url = f"{KRAKEN_BASE}/0/public/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    if result.get("error"):
        raise KrakenError(result["error"])
    return result["result"]


class KrakenError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════

def tg_send(text: str):
    """Send Telegram message. Silent fail if not configured."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(f"[TG skip] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        data = json.dumps({
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json"
        })
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[TG error] {e}")


# ═══════════════════════════════════════════════════════════════
#  PREFLIGHT CHECKS
# ═══════════════════════════════════════════════════════════════

def check_balance_usd() -> float:
    """Get available USD balance from Kraken."""
    balances = kraken_private("Balance")
    # Kraken uses ZUSD for US Dollar
    return float(balances.get("ZUSD", 0))


def get_asset_pair_info(pair: str) -> dict:
    """Get ordermin, lot_decimals for a pair."""
    data = kraken_public("AssetPairs", {"pair": pair})
    key = list(data.keys())[0]
    info = data[key]
    return {
        "ordermin": float(info.get("ordermin", 0)),
        "lot_decimals": int(info.get("lot_decimals", 8)),
        "pair_decimals": int(info.get("pair_decimals", 5)),
        "wsname": info.get("wsname", pair),
    }


def get_ticker_snapshot(pair: str) -> dict:
    """Get bid/ask/mid from Kraken Ticker."""
    data = kraken_public("Ticker", {"pair": pair})
    key = list(data.keys())[0]
    bid = float(data[key]["b"][0])
    ask = float(data[key]["a"][0])
    mid = (bid + ask) / 2
    return {"bid": bid, "ask": ask, "mid": mid}


# ═══════════════════════════════════════════════════════════════
#  CORE: EXECUTE ONE PAIR
# ═══════════════════════════════════════════════════════════════

def execute_pair(order: dict, settings: dict, today_chicago: str) -> dict:
    """
    Full two-phase execution for one trading pair.
    Returns execution result dict.
    """
    pair = order["pair"]
    amount = float(order["base_quote_amount"])
    dry_run = settings["dry_run"]
    cl_ord_id = f"dca-{pair}-{today_chicago}"

    print(f"\n{'='*50}")
    print(f"  {pair} | ${amount:.2f} | {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  cl_ord_id: {cl_ord_id}")

    # ── Phase 1: CLAIM ────────────────────────────────────────
    claim_row = {
        "trade_date_chicago": today_chicago,
        "pair": pair,
        "cl_ord_id": cl_ord_id,
        "status": "claimed",
        "requested_quote_amount_base": amount,
        "execution_started_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        sb_insert("dca_executions", claim_row)
        print(f"  ✓ Claimed")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print(f"  ⏭ Already claimed/executed today — skipping")
            return {"pair": pair, "status": "already_claimed", "skipped": True}
        raise

    # Helper to update this execution row
    def update_execution(updates: dict):
        sb_update(
            "dca_executions",
            {"cl_ord_id": f"eq.{cl_ord_id}"},
            updates,
        )

    # ── Preflight: Balance ────────────────────────────────────
    try:
        usd_balance = check_balance_usd()
        fee_buffer = amount * 0.01  # 1% buffer for fees
        needed = amount + fee_buffer
        print(f"  Balance: ${usd_balance:.2f} | Need: ${needed:.2f}")

        if usd_balance < needed:
            reason = f"USD balance ${usd_balance:.2f} < needed ${needed:.2f}"
            print(f"  ✗ {reason}")
            update_execution({
                "status": "skipped_insufficient_funds",
                "reason": reason,
                "execution_finished_at": datetime.now(timezone.utc).isoformat(),
            })
            tg_send(
                f"⚠️ <b>DCA SKIP</b>\n"
                f"{today_chicago} | {pair}\n"
                f"Insufficient funds: ${usd_balance:.2f} < ${needed:.2f}"
            )
            return {"pair": pair, "status": "skipped_insufficient_funds"}
    except KrakenError as e:
        print(f"  ⚠ Balance check failed: {e} — continuing anyway")

    # ── Preflight: Min order ──────────────────────────────────
    try:
        pair_info = get_asset_pair_info(pair)
        print(f"  Min order: {pair_info['ordermin']} | Lot decimals: {pair_info['lot_decimals']}")
    except KrakenError as e:
        reason = f"AssetPairs lookup failed: {e}"
        print(f"  ✗ {reason}")
        update_execution({
            "status": "failed_kraken",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
        })
        tg_send(f"❌ <b>DCA FAIL</b>\n{today_chicago} | {pair}\n{reason}")
        return {"pair": pair, "status": "failed_kraken"}

    # ── Snapshot: mid price ───────────────────────────────────
    try:
        ticker = get_ticker_snapshot(pair)
        print(f"  Bid: {ticker['bid']} | Ask: {ticker['ask']} | Mid: {ticker['mid']:.6f}")
        update_execution({
            "bid": ticker["bid"],
            "ask": ticker["ask"],
            "mid": ticker["mid"],
            "mid_ts": datetime.now(timezone.utc).isoformat(),
        })
    except KrakenError as e:
        print(f"  ⚠ Ticker failed: {e} — continuing without snapshot")
        ticker = {"bid": None, "ask": None, "mid": None}

    # ── Estimate volume for min-order check ───────────────────
    if ticker["mid"] and ticker["mid"] > 0:
        est_volume = amount / ticker["mid"]
        if est_volume < pair_info["ordermin"]:
            reason = (
                f"Estimated volume {est_volume:.8f} < "
                f"min {pair_info['ordermin']}"
            )
            print(f"  ✗ {reason}")
            update_execution({
                "status": "skipped_min_order",
                "reason": reason,
                "execution_finished_at": datetime.now(timezone.utc).isoformat(),
            })
            tg_send(
                f"⚠️ <b>DCA SKIP</b>\n"
                f"{today_chicago} | {pair}\n"
                f"Below min order: {reason}"
            )
            return {"pair": pair, "status": "skipped_min_order"}

    # ── Execute: Place order ──────────────────────────────────
    if dry_run:
        # Simulate fill with mid price
        sim_volume = (amount / ticker["mid"]) if ticker["mid"] else 0
        sim_fee = amount * 0.0026  # Kraken taker fee estimate

        print(f"  🧪 DRY RUN — simulated fill: {sim_volume:.8f} @ {ticker['mid']}")
        update_execution({
            "status": "filled_dry_run",
            "filled_quote_cost": amount,
            "fee_quote": round(sim_fee, 6),
            "filled_base_volume": round(sim_volume, pair_info["lot_decimals"]),
            "avg_price": ticker["mid"],
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
            "raw": json.dumps({"dry_run": True, "simulated_mid": ticker["mid"]}),
        })
        return {"pair": pair, "status": "filled_dry_run"}

    # ── LIVE ORDER ────────────────────────────────────────────
    try:
        order_params = {
            "pair": pair,
            "type": "buy",
            "ordertype": "market",
            "volume": str(amount),      # with viqc, this is quote currency
            "oflags": "viqc,fciq",      # volume-in-quote-currency, fee-in-quote
            "cl_ord_id": cl_ord_id,
        }

        result = kraken_private("AddOrder", order_params)
        order_id = result.get("txid", [None])[0]
        print(f"  ✓ Order placed: {order_id}")

        update_execution({
            "status": "placed",
            "order_id": order_id,
            "raw": json.dumps(result),
        })

    except KrakenError as e:
        reason = f"AddOrder failed: {e}"
        print(f"  ✗ {reason}")
        update_execution({
            "status": "failed_kraken",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
            "raw": json.dumps({"error": str(e)}),
        })
        tg_send(f"❌ <b>DCA FAIL</b>\n{today_chicago} | {pair}\n{reason}")
        return {"pair": pair, "status": "failed_kraken"}

    # ── Finalize: Query trade details ─────────────────────────
    time.sleep(2)  # small delay for order to settle

    try:
        finalize_order(cl_ord_id, order_id, pair_info)
    except Exception as e:
        print(f"  ⚠ Finalize failed: {e} — reconciliation will catch it")

    return {"pair": pair, "status": "filled", "order_id": order_id}


def finalize_order(cl_ord_id: str, order_id: str, pair_info: dict):
    """Query Kraken for fill details and update DB."""
    if not order_id:
        return

    trades = kraken_private("QueryOrders", {"txid": order_id, "trades": "true"})

    if order_id not in trades:
        print(f"  ⚠ Order {order_id} not found in QueryOrders yet")
        return

    order_data = trades[order_id]
    status = order_data.get("status", "")

    if status != "closed":
        print(f"  ⚠ Order status: {status} (not closed yet)")
        return

    cost = float(order_data.get("cost", 0))
    fee = float(order_data.get("fee", 0))
    vol_exec = float(order_data.get("vol_exec", 0))
    avg_px = float(order_data.get("price", 0))

    print(f"  Fill: {vol_exec} @ avg {avg_px} | cost ${cost} | fee ${fee}")

    sb_update(
        "dca_executions",
        {"cl_ord_id": f"eq.{cl_ord_id}"},
        {
            "status": "filled",
            "filled_quote_cost": cost,
            "fee_quote": fee,
            "filled_base_volume": vol_exec,
            "avg_price": avg_px,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
            "raw": json.dumps(order_data),
        },
    )


# ═══════════════════════════════════════════════════════════════
#  RECONCILIATION
# ═══════════════════════════════════════════════════════════════

def run_reconciliation():
    """Find stale claimed/placed executions and try to resolve them."""
    print("\n🔄 Reconciliation check...")

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()

    # Query stale rows
    stale = sb_get("dca_executions", {
        "status": "in.(claimed,placed)",
        "execution_started_at": f"lt.{cutoff}",
        "select": "cl_ord_id,order_id,pair,status,trade_date_chicago",
    })

    if not stale:
        print("  No stale executions found.")
        return

    for row in stale:
        cl_id = row["cl_ord_id"]
        order_id = row.get("order_id")
        print(f"  Stale: {cl_id} | status={row['status']} | order_id={order_id}")

        if row["status"] == "claimed" and not order_id:
            # Was claimed but never placed — likely crash before AddOrder
            # Try to find by cl_ord_id in Kraken (OpenOrders + ClosedOrders)
            found = try_find_kraken_order(cl_id)
            if found:
                print(f"    Found in Kraken! Finalizing...")
                pair_info = get_asset_pair_info(row["pair"])
                finalize_order(cl_id, found, pair_info)
            else:
                print(f"    Not found in Kraken — marking failed")
                sb_update(
                    "dca_executions",
                    {"cl_ord_id": f"eq.{cl_id}"},
                    {
                        "status": "failed_reconciliation",
                        "reason": "Claimed but no Kraken order found after timeout",
                        "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                tg_send(
                    f"🔄 <b>DCA RECONCILIATION</b>\n"
                    f"{row['trade_date_chicago']} | {row['pair']}\n"
                    f"Claimed but never placed — marked failed"
                )

        elif row["status"] == "placed" and order_id:
            # Was placed but never finalized — try to finalize now
            try:
                pair_info = get_asset_pair_info(row["pair"])
                finalize_order(cl_id, order_id, pair_info)
                print(f"    Finalized successfully")
            except Exception as e:
                print(f"    Finalize still failing: {e}")
                tg_send(
                    f"🔄 <b>DCA RECONCILIATION</b>\n"
                    f"{row['trade_date_chicago']} | {row['pair']}\n"
                    f"Placed but can't finalize: {e}"
                )


def try_find_kraken_order(cl_ord_id: str) -> str | None:
    """Try to find a Kraken order by cl_ord_id."""
    try:
        # Check closed orders (last 24h)
        closed = kraken_private("ClosedOrders", {"cl_ord_id": cl_ord_id})
        orders = closed.get("closed", {})
        for txid, order in orders.items():
            if order.get("cl_ord_id") == cl_ord_id:
                return txid
    except Exception as e:
        print(f"    ClosedOrders search failed: {e}")

    return None


# ═══════════════════════════════════════════════════════════════
#  WEEKLY SUMMARY
# ═══════════════════════════════════════════════════════════════

def send_weekly_summary():
    """Send Telegram weekly summary (idempotent, Sunday only)."""
    now_chicago = datetime.now(CHICAGO_TZ)

    # Only on Sundays
    if now_chicago.weekday() != 6:
        print("Not Sunday — skipping weekly summary")
        return

    # ISO week key for idempotency
    week_key = now_chicago.strftime("%G-W%V")

    # Check if already sent
    existing = sb_get("dca_notifications", {
        "notification_type": "eq.weekly_summary",
        "period_key": f"eq.{week_key}",
    })
    if existing:
        print(f"Weekly summary {week_key} already sent")
        return

    # Get this week's executions
    week_start = (now_chicago - timedelta(days=7)).strftime("%Y-%m-%d")
    today = now_chicago.strftime("%Y-%m-%d")

    rows = sb_get("dca_executions", {
        "trade_date_chicago": f"gte.{week_start}",
        "select": "pair,status,filled_quote_cost,fee_quote,filled_base_volume,avg_price,mid",
        "order": "trade_date_chicago.asc",
    })

    if not rows:
        print("No executions this week")
        return

    # Build summary per pair
    pairs = {}
    for r in rows:
        p = r["pair"]
        if p not in pairs:
            pairs[p] = {"filled": 0, "skipped": 0, "failed": 0,
                        "total_cost": 0, "total_fee": 0, "total_vol": 0,
                        "slippages": []}

        s = pairs[p]
        if "filled" in r["status"]:
            s["filled"] += 1
            s["total_cost"] += float(r.get("filled_quote_cost") or 0)
            s["total_fee"] += float(r.get("fee_quote") or 0)
            s["total_vol"] += float(r.get("filled_base_volume") or 0)
            mid = float(r.get("mid") or 0)
            avg = float(r.get("avg_price") or 0)
            if mid > 0 and avg > 0:
                s["slippages"].append((avg - mid) / mid * 100)
        elif "skipped" in r["status"]:
            s["skipped"] += 1
        elif "failed" in r["status"]:
            s["failed"] += 1

    # Format message
    lines = [f"📊 <b>DCA Weekly Summary</b>", f"Week: {week_key}", ""]

    for pair, s in pairs.items():
        symbol = pair.replace("USD", "")
        all_in = s["total_cost"] + s["total_fee"]
        avg_eff = (all_in / s["total_vol"]) if s["total_vol"] > 0 else 0
        avg_slip = (
            sum(s["slippages"]) / len(s["slippages"])
            if s["slippages"] else 0
        )

        lines.append(f"<b>{symbol}</b>")
        lines.append(
            f"  ✅ {s['filled']} filled"
            + (f" | ⏭ {s['skipped']} skip" if s["skipped"] else "")
            + (f" | ❌ {s['failed']} fail" if s["failed"] else "")
        )
        lines.append(f"  💰 ${all_in:.2f} invested | {s['total_vol']:.6f} {symbol}")
        if avg_eff > 0:
            lines.append(f"  📈 Avg price: ${avg_eff:.6f}")
        if avg_slip != 0:
            lines.append(f"  📐 Avg slippage: {avg_slip:.4f}%")
        lines.append("")

    msg = "\n".join(lines)
    tg_send(msg)

    # Mark as sent
    try:
        sb_insert("dca_notifications", {
            "notification_type": "weekly_summary",
            "period_key": week_key,
            "payload": json.dumps({"pairs": list(pairs.keys())}),
        })
    except Exception:
        pass  # idempotency — if it fails, next run will try again

    print(f"✓ Weekly summary sent for {week_key}")


# ═══════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "--reconcile":
        run_reconciliation()
        return

    if mode == "--weekly":
        send_weekly_summary()
        return

    # ── Default: scheduled run ────────────────────────────────
    now_chicago = datetime.now(CHICAGO_TZ)
    now_str = now_chicago.strftime("%Y-%m-%d %H:%M:%S %Z")
    today_chicago = now_chicago.strftime("%Y-%m-%d")

    print(f"🕐 Chicago time: {now_str}")

    # 1. Load settings
    settings_rows = sb_get("dca_settings", {"id": "eq.1"})
    if not settings_rows:
        print("✗ No dca_settings found!")
        sys.exit(1)
    settings = settings_rows[0]

    # 2. Check time window
    target_h, target_m = map(int, settings["target_time"].split(":"))
    window = settings["time_window_minutes"]

    target_dt = now_chicago.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    window_start = target_dt - timedelta(minutes=window // 2)
    window_end = target_dt + timedelta(minutes=window // 2)

    print(f"   Window: {window_start.strftime('%H:%M')} – {window_end.strftime('%H:%M')}")

    if not (window_start <= now_chicago <= window_end):
        print(f"   Outside window — exiting.")
        return

    print(f"   ✓ Inside window!")

    # 3. Reconciliation first
    run_reconciliation()

    # 4. Load enabled orders
    orders = sb_get("dca_orders", {
        "enabled": "eq.true",
        "base_quote_amount": "gt.0",
        "order": "priority.asc",
    })

    if not orders:
        print("   No enabled orders found.")
        return

    print(f"   Found {len(orders)} enabled order(s)")

    # 5. Execute each pair
    results = []
    for order in orders:
        try:
            result = execute_pair(order, settings, today_chicago)
            results.append(result)
        except Exception as e:
            print(f"   ✗ Unhandled error for {order['pair']}: {e}")
            tg_send(
                f"❌ <b>DCA CRASH</b>\n"
                f"{today_chicago} | {order['pair']}\n"
                f"Unhandled: {e}"
            )
            results.append({"pair": order["pair"], "status": "crashed", "error": str(e)})

        time.sleep(1)  # rate limit courtesy

    # 6. Weekly summary (if Sunday)
    send_weekly_summary()

    # 7. Print summary
    print(f"\n{'='*50}")
    print(f"📋 Run complete: {len(results)} pair(s)")
    for r in results:
        print(f"   {r['pair']}: {r['status']}")


if __name__ == "__main__":
    main()
