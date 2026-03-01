#!/usr/bin/env python3
"""
Strike DCA Bot v1.1 — Execution Engine (Kraken Architecture Port)
GitHub Actions → Python → Strike API → Supabase → Telegram
"""

import os
import sys
import time
import json
import uuid
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ohlc import build_daily_metrics

VERSION = "1.1.0-STRIKE"

ICONS = {
    "OK":        "\u2705",
    "WARN":      "\u26a0\ufe0f",
    "FAIL":      "\u274c",
    "SKIP":      "\u23ed",
    "DRYRUN":    "\U0001f9ea",
    "CHART":     "\U0001f4ca",
    "RECON":     "\U0001f504",
    "BOT":       "\U0001f916",
    "CLOCK":     "\U0001f550",
    "LIST":      "\U0001f4cb",
    "MONEY":     "\U0001f4b0",
    "TREND":     "\U0001f4c8",
    "RULER":     "\U0001f4d0",
}

STRIKE_API_KEY    = os.environ["STRIKE_API_KEY"]
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TG_BOT_TOKEN      = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "")

CHICAGO_TZ = ZoneInfo("America/Chicago")

USD_SAFETY_MARGIN = float(os.environ.get("DCA_USD_SAFETY_MARGIN", "0.03"))
TG_NOTIFY_ON_FILL = os.environ.get("TG_NOTIFY_ON_FILL", "true").lower() in ("1", "true", "yes", "y")


# ═══════════════════════════════════════════════════════════════
#  SUPABASE
# ═══════════════════════════════════════════════════════════════

def sb_request(method, path, body=None, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else None

def sb_get(table, params=None):
    return sb_request("GET", table, params=params)

def sb_insert(table, row):
    return sb_request("POST", table, body=row)

def sb_update(table, match_params, updates):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{urllib.parse.urlencode(match_params)}"
    data = json.dumps(updates).encode()
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else None


# ═══════════════════════════════════════════════════════════════
#  STRIKE API
# ═══════════════════════════════════════════════════════════════

class StrikeError(Exception):
    pass

def strike_request(method, endpoint, body=None):
    url = f"https://api.strike.me{endpoint}"
    headers = {
        "Authorization": f"Bearer {STRIKE_API_KEY}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode()
        raise StrikeError(f"HTTP {e.code}: {raw_err}")


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM & FORMATTERS
# ═══════════════════════════════════════════════════════════════

def tg_send(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(f"[TG skip] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[TG error] {e}")

def msg_ok(title, body):
    return f"{ICONS['OK']} <b>{title}</b>\n{body}"

def msg_warn(title, body):
    return f"{ICONS['WARN']} <b>{title}</b>\n{body}"

def msg_fail(title, body):
    return f"{ICONS['FAIL']} <b>{title}</b>\n{body}"

def msg_recon(title, body):
    return f"{ICONS['RECON']} <b>{title}</b>\n{body}"

def msg_dryrun(title, body):
    return f"{ICONS['DRYRUN']} <b>{title}</b>\n{body}"


# ═══════════════════════════════════════════════════════════════
#  PREFLIGHT
# ═══════════════════════════════════════════════════════════════

def check_balance_usd():
    balances = strike_request("GET", "/v1/balances")
    for b in (balances or []):
        if b.get("currency") == "USD":
            return float(b.get("available", 0))
    return 0.0

def get_ticker_snapshot(pair):
    target_crypto = pair.replace("USD", "")
    rates = strike_request("GET", "/v1/rates/ticker")
    for r in (rates or []):
        if r.get("sourceCurrency") == target_crypto and r.get("targetCurrency") == "USD":
            price = float(r.get("amount", 0))
            return {"bid": price, "ask": price, "mid": price}
    raise StrikeError(f"Rate for {pair} not found in ticker.")

def get_7d_ref_price(pair, user_id):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rows = sb_get("strike_dca_executions", {
            "user_id": f"eq.{user_id}",
            "pair": f"eq.{pair}",
            "select": "mid",
            "mid": "not.is.null",
            "execution_started_at": f"gte.{cutoff}",
            "order": "execution_started_at.desc",
        })
        if not rows:
            return None
        mids = [float(r["mid"]) for r in rows if r.get("mid")]
        return sum(mids) / len(mids) if mids else None
    except Exception as e:
        print(f"  {ICONS['WARN']} 7D ref price failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
#  FINALIZE
# ═══════════════════════════════════════════════════════════════

def _pair_from_cl_ord_id(cl_ord_id):
    try:
        parts = cl_ord_id.split("-")
        return parts[2] if len(parts) >= 3 else "?"
    except Exception:
        return "?"

def finalize_order(cl_ord_id, quote_id, ohlc_ctx=None, dry_run=False):
    if not quote_id:
        return

    final_q = None
    for attempt in range(6):
        try:
            q = strike_request("GET", f"/v1/currency-exchange-quotes/{quote_id}")
            state = (q.get("state") or "").lower()
            print(f"  Poll {attempt+1}: {state}")
            if state in ("completed", "executed", "settled", "filled"):
                amt = float((q.get("targetAmount") or {}).get("amount", 0))
                if amt > 0:
                    final_q = q
                    break
                print(f"  Poll {attempt+1}: completed but amount=0, retrying...")
            if state in ("failed", "expired", "canceled", "cancelled"):
                sb_update("strike_dca_executions", {"cl_ord_id": f"eq.{cl_ord_id}"}, {
                    "status": "failed_strike",
                    "reason": f"Quote state: {state}",
                    "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                })
                return
        except StrikeError as e:
            print(f"  {ICONS['WARN']} Poll error: {e}")
        time.sleep(3)

    if not final_q:
        print(f"  {ICONS['WARN']} Polling timeout for {quote_id}")
        return

    btc_received = float(final_q.get("targetAmount", {}).get("amount", 0))
    usd_spent    = float(final_q.get("sourceAmount", {}).get("amount", 0))
    avg_px       = usd_spent / btc_received if btc_received > 0 else 0

    print(f"  Fill: {btc_received:.8f} BTC @ ${avg_px:.2f} | cost ${usd_spent:.2f}")

    finished_at_utc = datetime.now(timezone.utc)
    sb_update(
        "strike_dca_executions",
        {"cl_ord_id": f"eq.{cl_ord_id}"},
        {
            "status": "filled",
            "filled_quote_cost": usd_spent,
            "fee_quote": 0,
            "filled_base_volume": btc_received,
            "avg_price": avg_px,
            "execution_finished_at": finished_at_utc.isoformat(),
            "raw": json.dumps(final_q),
            "h7": ohlc_ctx.get("H7") if ohlc_ctx and not dry_run else None,
            "h30": ohlc_ctx.get("H30") if ohlc_ctx and not dry_run else None,
            "ohlc_ts": datetime.now(timezone.utc).isoformat() if ohlc_ctx and not dry_run else None,
        },
    )

    if TG_NOTIFY_ON_FILL:
        pair = _pair_from_cl_ord_id(cl_ord_id)
        ts = finished_at_utc.astimezone(CHICAGO_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        tg_send(msg_ok(
            f"STRIKE {pair} FILLED",
            f"{ts}\nSpent: ${usd_spent:.2f}\nVol: {btc_received:.8f} BTC\nAvg: ${avg_px:.2f}"
        ))


# ═══════════════════════════════════════════════════════════════
#  EXECUTE ONE PAIR
# ═══════════════════════════════════════════════════════════════

def execute_pair(order, settings, today_chicago, user_id, force=False):
    pair         = order["pair"]
    total_target = float(order["base_quote_amount"])
    dry_run      = bool(settings["dry_run"])

    ts_ms = int(time.time() * 1000)
    if dry_run:
        cl_ord_id = f"strike-dca-{pair}-{today_chicago}-dry-{ts_ms}"
    elif force:
        cl_ord_id = f"strike-dca-{pair}-{today_chicago}-force-{ts_ms}"
    else:
        ot = order.get("target_time", "08:00").replace(":", "")
        cl_ord_id = f"strike-dca-{pair}-{today_chicago}-{ot}"

    print(f"\n{'='*50}")
    print(f"  {pair} | ${total_target:.2f} | {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  cl_ord_id: {cl_ord_id}")

    # CLAIM
    try:
        event_id = str(uuid.uuid4())
        sb_insert("strike_dca_executions", {
            "user_id": user_id,
            "trade_date_chicago": today_chicago,
            "pair": pair,
            "cl_ord_id": cl_ord_id,
            "status": "claimed",
            "requested_quote_amount_base": total_target,
            "execution_started_at": datetime.now(timezone.utc).isoformat(),
            "parent_event_id": event_id,
            "attempt_type": "market",
        })
        print(f"  {ICONS['OK']} Claimed")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print(f"  {ICONS['SKIP']} Already claimed — skipping")
            return {"pair": pair, "status": "already_claimed", "skipped": True}
        raw_err = e.read().decode()
        print(f"  {ICONS['FAIL']} Supabase insert error {e.code}: {raw_err}")
        raise

    def upd(updates):
        sb_update("strike_dca_executions", {"cl_ord_id": f"eq.{cl_ord_id}"}, updates)

    # BALANCE (skip in dry_run)
    try:
        usd_balance = check_balance_usd()
        print(f"  Balance: ${usd_balance:.2f} | Need: ${total_target:.2f}")
        if not dry_run and usd_balance < total_target:
            reason = f"USD balance ${usd_balance:.2f} < needed ${total_target:.2f}"
            upd({"status": "skipped_insufficient_funds", "reason": reason,
                 "execution_finished_at": datetime.now(timezone.utc).isoformat()})
            tg_send(msg_warn("STRIKE DCA SKIP",
                f"{today_chicago} | {pair}\nInsufficient funds: ${usd_balance:.2f} < ${total_target:.2f}"))
            return {"pair": pair, "status": "skipped_insufficient_funds"}
    except StrikeError as e:
        print(f"  {ICONS['WARN']} Balance check failed: {e} — continuing")

    # TICKER
    try:
        ticker = get_ticker_snapshot(pair)
        print(f"  Strike price: ${ticker['mid']:.2f}")
        upd({"bid": ticker["bid"], "ask": ticker["ask"], "mid": ticker["mid"],
             "mid_ts": datetime.now(timezone.utc).isoformat()})
    except StrikeError as e:
        print(f"  {ICONS['WARN']} Ticker failed: {e} — continuing without snapshot")
        ticker = {"bid": None, "ask": None, "mid": None}

    # -- OHLC Market Context ----------------------------------------
    ohlc_ctx: dict = {}
    if not dry_run and ticker["mid"] is not None:
        try:
            ohlc_ctx = build_daily_metrics(pair, days=220)
            _h7  = ohlc_ctx.get("H7")
            _h30 = ohlc_ctx.get("H30")
            if _h7 is not None and _h30 is not None:
                print(f"  OHLC H7={_h7:.6f} H30={_h30:.6f}")
            else:
                print("  OHLC partial/missing")
        except Exception as e:
            print(f"  WARN OHLC fetch failed: {e} -- continuing")
            ohlc_ctx = {}

    # 7D CAP
    CAP_PCT = 0.03
    if ticker["mid"] and not force:
        ref_price = get_7d_ref_price(pair, user_id)
        if ref_price is not None:
            cap_price = ref_price * (1 + CAP_PCT)
            print(f"  7D ref: ${ref_price:.2f} | Cap: ${cap_price:.2f} | Mid: ${ticker['mid']:.2f}")
            if ticker["mid"] > cap_price:
                pct_over = ((ticker["mid"] / ref_price) - 1) * 100
                reason = f"Mid ${ticker['mid']:.2f} > cap ${cap_price:.2f} (+{pct_over:.2f}% vs 7D ref)"
                print(f"  {ICONS['SKIP']} {reason}")
                upd({"status": "skipped_above_cap", "reason": reason,
                     "execution_finished_at": datetime.now(timezone.utc).isoformat()})
                tg_send(f"{ICONS['SKIP']} STRIKE {pair.replace('USD','')} +{pct_over:.2f}% virš cap — skip")
                return {"pair": pair, "status": "skipped_above_cap"}
            print(f"  {ICONS['OK']} Below cap — proceeding")
        else:
            print(f"  No 7D history — skipping cap check")

    # SAFE TOTAL
    safe_total = total_target - USD_SAFETY_MARGIN
    if safe_total <= 0:
        reason = f"Target too small after safety margin (${total_target:.2f} - ${USD_SAFETY_MARGIN:.2f})"
        upd({"status": "skipped_target_too_small", "reason": reason,
             "execution_finished_at": datetime.now(timezone.utc).isoformat()})
        tg_send(msg_warn("STRIKE DCA SKIP", f"{today_chicago} | {pair}\n{reason}"))
        return {"pair": pair, "status": "skipped_target_too_small"}

    target_crypto = pair.replace("USD", "")

    # DRY RUN
    if dry_run:
        est_vol = safe_total / ticker["mid"] if ticker["mid"] else 0
        print(f"  {ICONS['DRYRUN']} DRY RUN — est: {est_vol:.8f} BTC @ ${ticker['mid']}")
        upd({
            "status": "filled_dry_run",
            "filled_quote_cost": safe_total,
            "fee_quote": 0,
            "filled_base_volume": est_vol,
            "avg_price": ticker["mid"],
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
            "raw": json.dumps({"dry_run": True, "safe_total": safe_total}),
        })
        if TG_NOTIFY_ON_FILL:
            ts = datetime.now(timezone.utc).astimezone(CHICAGO_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
            tg_send(msg_dryrun(
                f"STRIKE {pair} DRY RUN",
                f"{ts}\nSpent est: ${safe_total:.2f}\nVol est: {est_vol:.8f} BTC\nPrice: ${ticker['mid']:.2f}"
            ))
        return {"pair": pair, "status": "filled_dry_run"}

    # LIVE
    try:
        quote_res = strike_request("POST", "/v1/currency-exchange-quotes", {
            "sell": "USD",
            "buy": target_crypto,
            "amount": {"currency": "USD", "amount": f"{safe_total:.2f}"}
        })
        quote_id = quote_res.get("id") or quote_res.get("quoteId")
        if not quote_id:
            raise StrikeError(f"Missing quoteId in response: {quote_res}")

        print(f"  {ICONS['OK']} Quote: {quote_id}")
        upd({"status": "quote_placed", "order_id": quote_id})

        strike_request("PATCH", f"/v1/currency-exchange-quotes/{quote_id}/execute")
        print(f"  {ICONS['OK']} Quote executed")
        upd({"status": "placed", "raw": json.dumps(quote_res)})

    except StrikeError as e:
        reason = f"Strike API failed: {e}"
        print(f"  {ICONS['FAIL']} {reason}")
        upd({"status": "failed_strike", "reason": reason,
             "execution_finished_at": datetime.now(timezone.utc).isoformat()})
        tg_send(msg_fail("STRIKE DCA FAIL", f"{today_chicago} | {pair}\n{reason}"))
        return {"pair": pair, "status": "failed_strike"}

    time.sleep(1)

    try:
        finalize_order(cl_ord_id, quote_id, ohlc_ctx=ohlc_ctx, dry_run=dry_run)
    except Exception as e:
        print(f"  {ICONS['WARN']} finalize_order error: {e}")

    return {"pair": pair, "status": "placed", "order_id": quote_id}


# ═══════════════════════════════════════════════════════════════
#  RECONCILIATION
# ═══════════════════════════════════════════════════════════════

def run_reconciliation(user_id):
    print(f"\n{ICONS['RECON']} Reconciliation check...")
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    stale = sb_get("strike_dca_executions", {
        "user_id": f"eq.{user_id}",
        "status": "in.(quote_placed,placed)",
        "execution_started_at": f"lt.{cutoff}",
        "select": "cl_ord_id,order_id,pair,status,trade_date_chicago",
    })
    if not stale:
        print("  No stale executions found.")
        return
    for row in stale:
        cl_id    = row["cl_ord_id"]
        quote_id = row.get("order_id")
        print(f"  Stale: {cl_id} | status={row['status']} | quote={quote_id}")
        if quote_id:
            try:
                finalize_order(cl_id, quote_id)
                print("    Finalized successfully")
            except Exception as e:
                print(f"    Finalize still failing: {e}")
                sb_update("strike_dca_executions", {"cl_ord_id": f"eq.{cl_id}"}, {
                    "status": "failed_reconciliation",
                    "reason": str(e),
                    "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                })
                tg_send(msg_recon("STRIKE RECONCILIATION",
                    f"{row['trade_date_chicago']} | {row['pair']}\nCan t finalize: {e}\nManual check required."))


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    print(f"{ICONS['BOT']} Strike DCA Bot v{VERSION}")

    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    settings_rows = sb_get("strike_dca_settings", {"id": "eq.9ec66057-9db9-4a61-b1e2-d2d4aa045b24"})
    if not settings_rows:
        print(f"{ICONS['FAIL']} No strike_dca_settings found!")
        sys.exit(1)
    settings = settings_rows[0]

    user_id = settings.get("user_id")
    if not user_id:
        print(f"{ICONS['FAIL']} strike_dca_settings.user_id is missing!")
        sys.exit(1)

    print(f"   user_id: {user_id[:8]}...")

    if mode == "--reconcile":
        run_reconciliation(user_id)
        return

    now_chicago   = datetime.now(CHICAGO_TZ)
    today_chicago = now_chicago.strftime("%Y-%m-%d")
    print(f"{ICONS['CLOCK']} Chicago time: {now_chicago.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    run_reconciliation(user_id)

    orders = sb_get("strike_dca_orders", {
        "user_id": f"eq.{user_id}",
        "enabled": "eq.true",
        "base_quote_amount": "gt.0",
        "order": "priority.asc",
    })

    if not orders:
        print("   No enabled orders found.")
        return

    print(f"   Found {len(orders)} enabled order(s)")

    results = []
    for order in orders:
        pair     = order["pair"]
        o_time   = order.get("target_time") or settings.get("target_time", "08:00")
        o_window = int(order.get("time_window_minutes") or settings.get("time_window_minutes", 10))

        o_h, o_m  = map(int, o_time.split(":"))
        o_target  = now_chicago.replace(hour=o_h, minute=o_m, second=0, microsecond=0)
        o_start   = o_target - timedelta(minutes=o_window // 2)
        o_end     = o_target + timedelta(minutes=o_window // 2)
        in_window = o_start <= now_chicago <= o_end

        print(f"\n   {pair} @ {o_time} CT [{o_start.strftime('%H:%M')}-{o_end.strftime('%H:%M')}] {'IN' if in_window else 'OUT'}")

        if mode != "--force" and not in_window:
            print(f"   {ICONS['SKIP']} Outside window — skipping {pair}")
            continue

        try:
            result = execute_pair(order, settings, today_chicago, user_id, force=(mode == "--force"))
            results.append(result)
        except Exception as e:
            print(f"   {ICONS['FAIL']} Unhandled error for {pair}: {e}")
            tg_send(msg_fail("STRIKE DCA CRASH", f"{today_chicago} | {pair}\nUnhandled: {e}"))
            results.append({"pair": pair, "status": "crashed", "error": str(e)})

        time.sleep(1)

    print(f"\n{'='*50}")
    print(f"{ICONS['LIST']} Run complete: {len(results)} pair(s)")
    for r in results:
        print(f"   {r['pair']}: {r['status']}")


if __name__ == "__main__":
    main()
