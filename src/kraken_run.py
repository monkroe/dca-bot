#!/usr/bin/env python3
"""
Kraken DCA Bot v1.1 — Execution Engine
GitHub Actions → Python → Kraken API → Supabase → Telegram

Usage (via GH Actions, not directly):
  python src/dca_run.py              # Main scheduled run
  python src/dca_run.py --reconcile  # Reconciliation only
  python src/dca_run.py --weekly     # Weekly summary only

Control-plane:
- Telegram/BENAS inserts command into Supabase dca_commands (status='queued')
- This script, on each run, picks up queued commands, applies to dca_settings/dca_orders,
  and marks command done/failed.

Changelog v1.1:
- user_id propagated to all dca_executions inserts
- user_id filters on reconciliation and weekly summary queries
- user_id filter on dca_orders load
- VERSION constant for GH Actions log traceability
- ICONS constants layer (zero inline emoji)
- Settings re-read after command processing
"""

import hashlib
import uuid
import hmac
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ohlc import build_daily_metrics

VERSION = "1.1.0"

# ═══════════════════════════════════════════════════════════════
#  ICONS — single source of truth for all UI symbols
#  Change here → changes everywhere. Set all to "" for plain mode.
# ═══════════════════════════════════════════════════════════════

ICONS = {
    "OK":        "\u2705",   # ✅
    "WARN":      "\u26a0\ufe0f",  # ⚠️
    "FAIL":      "\u274c",   # ❌
    "SKIP":      "\u23ed",   # ⏭
    "DRYRUN":    "\U0001f9ea",  # 🧪
    "CHART":     "\U0001f4ca",  # 📊
    "RECON":     "\U0001f504",  # 🔄
    "BOT":       "\U0001f916",  # 🤖
    "CLOCK":     "\U0001f550",  # 🕐
    "LIST":      "\U0001f4cb",  # 📋
    "MONEY":     "\U0001f4b0",  # 💰
    "TREND":     "\U0001f4c8",  # 📈
    "RULER":     "\U0001f4d0",  # 📐
}

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

# Safety margin in quote currency (USD) to ensure all-in never exceeds target.
# Covers: price drift, fee_rate mismatch, rounding drift.
USD_SAFETY_MARGIN = float(os.environ.get("DCA_USD_SAFETY_MARGIN", "0"))

# Whether to send Telegram message on successful fills (dry-run and live).
TG_NOTIFY_ON_FILL = os.environ.get("TG_NOTIFY_ON_FILL", "true").lower() in ("1", "true", "yes", "y")

# Commands (SQL control-plane)
DCA_COMMANDS_MAX_PER_RUN = int(os.environ.get("DCA_COMMANDS_MAX_PER_RUN", "25"))

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

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else None


def sb_get(table: str, params: dict | None = None):
    return sb_request("GET", table, params=params)


def sb_insert(table: str, row: dict):
    """Insert row, return inserted row. Raises on conflict."""
    return sb_request("POST", table, body=row)


def sb_update(table: str, match_params: dict, updates: dict):
    """Update rows matching filter."""
    url = f"{SUPABASE_URL}/rest/v1/{table}?{urllib.parse.urlencode(match_params)}"
    data = json.dumps(updates).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="PATCH",
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

def save_mid_snapshot(pair: str, ticker: dict) -> None:
    """Save ticker snapshot for A/B comparison and historical analysis."""
    try:
        sb_insert("dca_mid_snapshots", {
            "pair": pair,
            "mid": ticker.get("mid"),
            "bid": ticker.get("bid"),
            "ask": ticker.get("ask"),
        })
    except Exception as e:
        print(f"  {ICONS['WARN']} snapshot save failed: {e}")

def get_7d_ref_price(pair: str, user_id: str) -> float | None:
    """Get average mid price from all executions (filled + skipped) in last 7 days."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rows = sb_get("dca_executions", {
            "user_id": f"eq.{user_id}",
            "pair": f"eq.{pair}",
            "select": "mid",
            "mid": "not.is.null",
            "execution_started_at": f"gte.{cutoff}",
            "order": "execution_started_at.desc",
        })
        if not rows or len(rows) == 0:
            return None
        mids = [float(r["mid"]) for r in rows if r.get("mid")]
        if not mids:
            return None
        return sum(mids) / len(mids)
    except Exception as e:
        print(f"  {ICONS['WARN']} 7D ref price failed: {e}")
        return None

#  TELEGRAM
# ═══════════════════════════════════════════════════════════════

def _tg_html(text: str) -> str:
    # Telegram parse_mode=HTML is strict. Escape user/variable content, but keep our simple formatting tags.
    t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Allowlist: only <b>...</b> (used by msg_ok/msg_warn/msg_fail/msg_recon/msg_dryrun)
    t = t.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    return t

def tg_send(text: str):
    """Send Telegram message. Silent fail if not configured."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(f"[TG skip] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        data = json.dumps(
            {"chat_id": TG_CHAT_ID, "text": _tg_html(text), "parse_mode": "HTML"}
        ).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = "<no body>"
        body = body[:500]
        print("[TG error] HTTP " + str(e.code) + ": " + str(e.reason) + " | " + body)
    except Exception as e:
        print("[TG error] " + str(e))


# ═══════════════════════════════════════════════════════════════
#  MESSAGE FORMATTERS — semantic UI layer
# ═══════════════════════════════════════════════════════════════

def msg_ok(title: str, body: str) -> str:
    return f"{ICONS['OK']} <b>{title}</b>\n{body}"


def msg_warn(title: str, body: str) -> str:
    return f"{ICONS['WARN']} <b>{title}</b>\n{body}"


def msg_fail(title: str, body: str) -> str:
    return f"{ICONS['FAIL']} <b>{title}</b>\n{body}"


def msg_recon(title: str, body: str) -> str:
    return f"{ICONS['RECON']} <b>{title}</b>\n{body}"


def msg_dryrun(title: str, body: str) -> str:
    return f"{ICONS['DRYRUN']} <b>{title}</b>\n{body}"


# ═══════════════════════════════════════════════════════════════
#  SQL CONTROL-PLANE (dca_commands)
# ═══════════════════════════════════════════════════════════════

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_load(x):
    if x is None:
        return None
    if isinstance(x, (dict, list)):
        return x
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:
            return None
    return None


def _claim_command(cmd_id: int) -> dict | None:
    """
    Optimistic claim: set status=processing only if status is queued.
    Returns updated row (representation) if claimed, else None.
    """
    try:
        res = sb_update(
            "dca_commands",
            {"id": f"eq.{cmd_id}", "status": "eq.queued"},
            {"status": "processing"},
        )
        if isinstance(res, list) and len(res) == 1:
            return res[0]
        return None
    except Exception as e:
        print(f"[commands] claim failed id={cmd_id}: {e}")
        return None


def _set_command_status(cmd_id: int, status: str):
    """Best-effort: update status only (to avoid schema mismatch)."""
    try:
        sb_update("dca_commands", {"id": f"eq.{cmd_id}"}, {"status": status})
    except Exception as e:
        print(f"[commands] set status failed id={cmd_id} -> {status}: {e}")


def _apply_settings_set_timewindow(payload: dict):
    """
    payload example:
      {"timezone":"America/Chicago","target_time":"11:50","time_window_minutes":480}
    Applies to dca_settings id=1.
    """
    tz = payload.get("timezone")
    tt = payload.get("target_time")
    tw = payload.get("time_window_minutes")

    updates = {"updated_at": _now_utc_iso()}

    if isinstance(tz, str) and tz:
        updates["timezone"] = tz

    if isinstance(tt, str) and tt and ":" in tt:
        h, m = tt.split(":", 1)
        if h.isdigit() and m.isdigit():
            hh = int(h); mm = int(m)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                updates["target_time"] = f"{hh:02d}:{mm:02d}"

    if tw is not None:
        try:
            n = int(tw)
            if n > 0:
                updates["time_window_minutes"] = n
        except Exception:
            pass

    sb_update("dca_settings", {"id": "eq.1"}, updates)


def process_dca_commands(max_per_run: int = 25):
    """
    Pull queued commands and apply them.
    Important: apply BEFORE reading dca_settings for this run.
    """
    try:
        rows = sb_get(
            "dca_commands",
            {
                "status": "eq.queued",
                "select": "id,command,payload,created_at,status",
                "order": "created_at.asc",
                "limit": str(max_per_run),
            },
        ) or []
    except Exception as e:
        print(f"[commands] load failed: {e}")
        return

    if not rows:
        print("[commands] no queued commands")
        return

    print(f"[commands] queued: {len(rows)} (max {max_per_run})")

    for r in rows:
        cmd_id = r.get("id")
        cmd = r.get("command")
        payload_raw = r.get("payload")

        if not isinstance(cmd_id, int):
            continue

        claimed = _claim_command(cmd_id)
        if not claimed:
            print(f"[commands] skip (not claimed) id={cmd_id}")
            continue

        payload = _safe_json_load(payload_raw) or {}
        if not isinstance(payload, dict):
            payload = {}

        print(f"[commands] processing id={cmd_id} cmd={cmd}")

        try:
            if cmd == "settings_set_timewindow":
                _apply_settings_set_timewindow(payload)
                _set_command_status(cmd_id, "done")
                print(f"[commands] done id={cmd_id}")
            else:
                print(f"[commands] unknown cmd id={cmd_id}: {cmd}")
                _set_command_status(cmd_id, "failed")
        except Exception as e:
            print(f"[commands] failed id={cmd_id}: {e}")
            _set_command_status(cmd_id, "failed")


# ═══════════════════════════════════════════════════════════════
#  NUMERIC HELPERS
# ═══════════════════════════════════════════════════════════════

def floor_to_decimals(x: float, decimals: int) -> float:
    """Floor (truncate) to N decimals to avoid overspending due to rounding."""
    if decimals < 0:
        return x
    factor = 10 ** decimals
    return int(x * factor) / factor


def format_volume(v: float, lot_decimals: int) -> str:
    """Format volume with exact lot_decimals to avoid float string artifacts."""
    if lot_decimals <= 0:
        return str(int(v))
    return f"{v:.{lot_decimals}f}"


# ═══════════════════════════════════════════════════════════════
#  KRAKEN API (zero dependencies)
# ═══════════════════════════════════════════════════════════════

KRAKEN_BASE = "https://api.kraken.com"


class KrakenError(Exception):
    pass


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


# ═══════════════════════════════════════════════════════════════
#  PREFLIGHT CHECKS
# ═══════════════════════════════════════════════════════════════

def check_balance_usd() -> float:
    """Get available USD balance from Kraken."""
    balances = kraken_private("Balance")
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
#  FINALIZE (LIVE)
# ═══════════════════════════════════════════════════════════════

def _pair_from_cl_ord_id(cl_ord_id: str) -> str:
    """Extract pair name from cl_ord_id format: dca-KASUSD-YYYY-MM-DD..."""
    try:
        parts = cl_ord_id.split("-")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    except Exception:
        pass
    return "?"


def finalize_order(cl_ord_id: str, order_id: str, mid: float | None = None, ohlc_ctx: dict | None = None):
    """Query Kraken for fill details and update DB."""
    if not order_id:
        return

    trades = kraken_private("QueryOrders", {"txid": order_id, "trades": "true"})

    if order_id not in trades:
        print(f"  {ICONS['WARN']} Order {order_id} not found in QueryOrders yet")
        return

    order_data = trades[order_id]
    status = order_data.get("status", "")

    if status != "closed":
        print(f"  {ICONS['WARN']} Order status: {status} (not closed yet)")
        return

    cost = float(order_data.get("cost", 0))
    fee = float(order_data.get("fee", 0))
    vol_exec = float(order_data.get("vol_exec", 0))
    avg_px = float(order_data.get("price", 0))

    all_in = cost + fee
    print(f"  Fill: {vol_exec} @ avg {avg_px} | cost ${cost} | fee ${fee} | all-in ${all_in:.4f}")

    finished_at_utc = datetime.now(timezone.utc)
    finished_at_iso = finished_at_utc.isoformat()

    # ── bps metrics (Phase 1.5) ──────────────────────────────
    impact_bps = None
    all_in_bps = None
    mid_source = None
    if mid is not None and mid > 0 and vol_exec > 0 and avg_px > 0:
        mid_source = "ticker_fallback"
        impact_bps = round(((avg_px / mid) - 1) * 10_000, 4)
        all_in_price = (cost + fee) / vol_exec
        all_in_bps = round(((all_in_price / mid) - 1) * 10_000, 4)
        print(f"  impact_bps: {impact_bps:.4f} | all_in_bps: {all_in_bps:.4f} | mid_source: {mid_source}")

    sb_update(
        "dca_executions",
        {"cl_ord_id": f"eq.{cl_ord_id}"},
        {
            "status": "filled",
            "filled_quote_cost": cost,
            "fee_quote": fee,
            "filled_base_volume": vol_exec,
            "avg_price": avg_px,
            "execution_finished_at": finished_at_iso,
            "raw": json.dumps(order_data),
            "impact_bps": impact_bps,
            "all_in_bps": all_in_bps,
            "mid_source": mid_source,
            "h7": ohlc_ctx.get("H7") if ohlc_ctx else None,
            "h30": ohlc_ctx.get("H30") if ohlc_ctx else None,
            "ohlc_ts": datetime.now(timezone.utc).isoformat() if ohlc_ctx else None,
        },
    )

    if TG_NOTIFY_ON_FILL:
        pair = None
        try:
            descr = order_data.get("descr") or {}
            pair = descr.get("pair")
        except Exception:
            pair = None
        if not pair:
            pair = _pair_from_cl_ord_id(cl_ord_id)

        filled_ts = finished_at_utc.astimezone(CHICAGO_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

        impact_line = ""
        if impact_bps is not None:
            sign = "+" if float(impact_bps) >= 0 else ""
            impact_line = f"\nImpact: {sign}{float(impact_bps):.1f} bps"
        all_in_line = ""
        if all_in_bps is not None:
            sign = "+" if float(all_in_bps) >= 0 else ""
            all_in_line = f" | All-in: {sign}{float(all_in_bps):.1f} bps"
        ohlc_line = ""
        if ohlc_ctx is not None and (ohlc_ctx.get("H7") is not None or ohlc_ctx.get("H30") is not None):
            _h7 = ohlc_ctx.get("H7")
            _h30 = ohlc_ctx.get("H30")
            parts = []
            if _h7 is not None:
                parts.append(f"H7: ${float(_h7):.4f}")
            if _h30 is not None:
                parts.append(f"H30: ${float(_h30):.4f}")
            if parts:
                ohlc_line = "\n" + " | ".join(parts)
        symbol = pair.replace("USD", "")
        tg_send(msg_ok(
            f"DCA {pair} FILLED",
            f"{filled_ts}\n\n"
            f"Amount: {vol_exec} {symbol}\n"
            f"Price:  ${avg_px:.5f}\n"
            f"Cost:   ${cost:.4f}\n"
            f"Fee:    ${fee:.4f}\n"
            f"Total:  ${all_in:.4f}"
            f"{impact_line}{all_in_line}{ohlc_line}\n"
            f"Mid:    {mid_source or 'unknown'}"
        ))


# ═══════════════════════════════════════════════════════════════
#  CORE: EXECUTE ONE PAIR
# ═══════════════════════════════════════════════════════════════

def execute_pair(order: dict, settings: dict, today_chicago: str, user_id: str, force: bool = False) -> dict:
    """
    Full two-phase execution for one trading pair.

    Bulletproof fee-aware volume calculation (target = all-in cap):
      total_target  = base_quote_amount (e.g. $10.00)
      safe_total    = max(total_target - USD_SAFETY_MARGIN, 0)
      cost_target   = safe_total / (1 + taker_fee_rate)
      base_volume   = floor(cost_target / ask_price, lot_decimals)

    This ensures:  cost + fee  <=  safe_total  <= total_target
    """
    pair = order["pair"]
    total_target = float(order["base_quote_amount"])
    fee_rate = float(settings["taker_fee_rate"])
    dry_run = bool(settings["dry_run"])

    if dry_run:
        cl_ord_id = f"dca-{pair}-{today_chicago}-dry-{int(time.time() * 1000)}"
    else:
        ot = order.get("target_time", "08:00").replace(":", "")
        cl_ord_id = f"dca-{pair}-{today_chicago}-force-{int(time.time() * 1000)}" if force else f"dca-{pair}-{today_chicago}-{ot}"

    print(f"\n{'='*50}")
    print(f"  {pair} | ${total_target:.2f} | fee {fee_rate*100:.2f}% | {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  cl_ord_id: {cl_ord_id}")

    # ── Phase 1: CLAIM ────────────────────────────────────────
    event_id = str(uuid.uuid4())
    claim_row = {
        "user_id": user_id,
        "trade_date_chicago": today_chicago,
        "pair": pair,
        "cl_ord_id": cl_ord_id,
        "status": "claimed",
        "requested_quote_amount_base": total_target,
        "execution_started_at": datetime.now(timezone.utc).isoformat(),
        "parent_event_id": event_id,
        "attempt_type": "market",
    }

    try:
        sb_insert("dca_executions", claim_row)
        print(f"  {ICONS['OK']} Claimed")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            try:
                raw = e.read().decode()
                print(f"[Supabase HTTPError 409] {raw}")
            except Exception:
                pass
            print(f"  {ICONS['SKIP']} Already claimed/executed for this cl_ord_id — skipping")
            return {"pair": pair, "status": "already_claimed", "skipped": True}
        raise

    def update_execution(updates: dict):
        sb_update("dca_executions", {"cl_ord_id": f"eq.{cl_ord_id}"}, updates)

    # ── Preflight: Balance ────────────────────────────────────
    try:
        usd_balance = check_balance_usd()
        print(f"  Balance: ${usd_balance:.2f} | Need: ${total_target:.2f}")

        if usd_balance < total_target:
            reason = f"USD balance ${usd_balance:.2f} < needed ${total_target:.2f}"
            print(f"  {ICONS['FAIL']} {reason}")
            update_execution({
                "status": "skipped_insufficient_funds",
                "reason": reason,
                "execution_finished_at": datetime.now(timezone.utc).isoformat(),
            })
            ts_chi = datetime.now(timezone.utc).astimezone(CHICAGO_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
            tg_send(msg_warn(
                "DCA SKIP",
                f"{ts_chi} | {pair}\nInsufficient funds: ${usd_balance:.2f} < ${total_target:.2f}"
            ))
            return {"pair": pair, "status": "skipped_insufficient_funds"}
    except KrakenError as e:
        print(f"  {ICONS['WARN']} Balance check failed: {e} — continuing anyway")

    # ── Preflight: Pair info ──────────────────────────────────
    try:
        pair_info = get_asset_pair_info(pair)
        print(f"  Min order: {pair_info['ordermin']} | Lot decimals: {pair_info['lot_decimals']}")
    except KrakenError as e:
        reason = f"AssetPairs lookup failed: {e}"
        print(f"  {ICONS['FAIL']} {reason}")
        update_execution({
            "status": "failed_kraken",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
        })
        tg_send(msg_fail("DCA FAIL", f"{today_chicago} | {pair}\n{reason}"))
        return {"pair": pair, "status": "failed_kraken"}

    # ── Snapshot: bid/ask/mid ─────────────────────────────────
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
        print(f"  {ICONS['WARN']} Ticker failed: {e} — continuing without snapshot")
        ticker = {"bid": None, "ask": None, "mid": None}


    # -- OHLC Market Context (Phase 1.5) ----------------------------
    ohlc_ctx: dict = {}
    if ticker["mid"] is not None:
        try:
            ohlc_ctx = build_daily_metrics(pair, days=220)
            h7  = ohlc_ctx.get("H7")
            h30 = ohlc_ctx.get("H30")
            if h7 is not None and h30 is not None:
                print(f"  OHLC H7={h7:.6f} H30={h30:.6f} latest={ohlc_ctx.get('latest_close', 0.0):.6f}")
            else:
                print("  OHLC partial/missing")
        except Exception as e:
            print(f"  WARN OHLC fetch failed: {e} -- continuing without market context")
            ohlc_ctx = {}

    # ── 7D Cap Check (Smart DCA) ───────────────────────────────
    CAP_PCT = 0.03  # 3.0%
    if ticker["mid"] and not force:
        ref_price = get_7d_ref_price(pair, user_id)
        if ref_price is not None:
            cap_price = ref_price * (1 + CAP_PCT)
            print(f"  7D ref: ${ref_price:.6f} | Cap: ${cap_price:.6f} | Mid: ${ticker['mid']:.6f}")
            if ticker["mid"] > cap_price:
                pct_over = ((ticker["mid"] / ref_price) - 1) * 100
                reason = f"Mid ${ticker['mid']:.6f} > cap ${cap_price:.6f} (+{pct_over:.2f}% vs 7D ref)"
                print(f"  {ICONS['SKIP']} {reason}")
                update_execution({
                    "status": "skipped_above_cap",
                    "reason": reason,
                    "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                })
                tg_send(f"{ICONS['SKIP']} {pair.replace('USD','')} +{pct_over:.2f}% virš cap — skip")
                return {"pair": pair, "status": "skipped_above_cap"}
            print(f"  {ICONS['OK']} Below cap — proceeding")
        else:
            print(f"  No 7D history — skipping cap check")
    # ── Fee-aware volume calculation ──────────────────────────
    if not ticker["ask"] or ticker["ask"] <= 0:
        reason = "No valid ASK price — can't compute base volume"
        print(f"  {ICONS['FAIL']} {reason}")
        update_execution({
            "status": "failed_kraken",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
        })
        tg_send(msg_fail("DCA FAIL", f"{today_chicago} | {pair}\n{reason}"))
        return {"pair": pair, "status": "failed_kraken"}

    if fee_rate < 0:
        print(f"  {ICONS['WARN']} fee_rate < 0 ({fee_rate}) — clamping to 0")
        fee_rate = 0.0

    safe_total = total_target - USD_SAFETY_MARGIN
    if safe_total < 0:
        safe_total = 0.0

    denom = 1.0 + fee_rate
    if denom <= 0:
        denom = 1.0

    cost_target = safe_total / denom

    if cost_target <= 0:
        reason = f"Target too small after safety margin (${total_target:.2f} - ${USD_SAFETY_MARGIN:.2f})"
        print(f"  {ICONS['FAIL']} {reason}")
        update_execution({
            "status": "skipped_target_too_small",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
        })
        tg_send(msg_warn("DCA SKIP", f"{today_chicago} | {pair}\n{reason}"))
        return {"pair": pair, "status": "skipped_target_too_small"}

    base_volume = floor_to_decimals(cost_target / ticker["ask"], pair_info["lot_decimals"])

    if base_volume <= 0:
        reason = (
            f"Computed base_volume is 0 after rounding "
            f"(cost_target={cost_target:.6f}, ask={ticker['ask']}, lot_decimals={pair_info['lot_decimals']})"
        )
        print(f"  {ICONS['FAIL']} {reason}")
        update_execution({
            "status": "skipped_target_too_small",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
        })
        tg_send(msg_warn("DCA SKIP", f"{today_chicago} | {pair}\n{reason}"))
        return {"pair": pair, "status": "skipped_target_too_small"}

    estimated_cost = base_volume * ticker["ask"]
    estimated_fee = estimated_cost * fee_rate
    estimated_total = estimated_cost + estimated_fee

    print(f"  Fee-aware calc (with safety margin):")
    print(f"    total_target:   ${total_target:.4f}")
    print(f"    safety_margin:  ${USD_SAFETY_MARGIN:.4f}")
    print(f"    safe_total:     ${safe_total:.4f}")
    print(f"    cost_target:    ${cost_target:.4f}")
    print(f"    ask:            ${ticker['ask']}")
    print(f"    base_volume:    {base_volume}")
    print(f"    est. cost:      ${estimated_cost:.4f}")
    print(f"    est. fee:       ${estimated_fee:.4f}")
    print(f"    est. total:     ${estimated_total:.4f}")

    # ── Min order check ───────────────────────────────────────
    if base_volume < pair_info["ordermin"]:
        reason = f"Base volume {base_volume} < min {pair_info['ordermin']}"
        print(f"  {ICONS['FAIL']} {reason}")
        update_execution({
            "status": "skipped_min_order",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
        })
        tg_send(msg_warn(
            "DCA SKIP",
            f"{today_chicago} | {pair}\nBelow min order: {reason}"
        ))
        return {"pair": pair, "status": "skipped_min_order"}

    # ── Execute ───────────────────────────────────────────────
    if dry_run:
        print(f"  {ICONS['DRYRUN']} DRY RUN — simulated fill: {base_volume} @ ask {ticker['ask']}")
        update_execution({
            "status": "filled_dry_run",
            "filled_quote_cost": round(estimated_cost, 6),
            "fee_quote": round(estimated_fee, 6),
            "filled_base_volume": base_volume,
            "avg_price": ticker["ask"],
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
            "raw": json.dumps({
                "dry_run": True,
                "total_target": total_target,
                "safety_margin": USD_SAFETY_MARGIN,
                "safe_total": round(safe_total, 6),
                "fee_rate": fee_rate,
                "cost_target": round(cost_target, 6),
                "ask": ticker["ask"],
                "base_volume": base_volume,
                "estimated_cost": round(estimated_cost, 6),
                "estimated_fee": round(estimated_fee, 6),
                "estimated_total": round(estimated_total, 6),
            }),
        })

        if TG_NOTIFY_ON_FILL:
            ts = datetime.now(timezone.utc).astimezone(CHICAGO_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
            tg_send(msg_dryrun(
                f"DCA {pair} DRY RUN FILLED",
                f"{ts}\nAll-in est: ${estimated_total:.4f}\nVol est: {base_volume}\nAsk: ${ticker['ask']}"
            ))

        return {"pair": pair, "status": "filled_dry_run"}

    # ── LIVE ORDER ────────────────────────────────────────────
    try:
        vol_str = format_volume(base_volume, pair_info["lot_decimals"])

        order_params = {
            "pair": pair,
            "type": "buy",
            "ordertype": "market",
            "volume": vol_str,
            "oflags": "fciq",
            "cl_ordid": cl_ord_id,
        }

        result = kraken_private("AddOrder", order_params)
        order_id = result.get("txid", [None])[0]
        print(f"  {ICONS['OK']} Order placed: {order_id}")

        update_execution({
            "status": "placed",
            "order_id": order_id,
            "raw": json.dumps(result),
        })

    except KrakenError as e:
        reason = f"AddOrder failed: {e}"
        print(f"  {ICONS['FAIL']} {reason}")
        update_execution({
            "status": "failed_kraken",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
            "raw": json.dumps({"error": str(e)}),
        })
        tg_send(msg_fail("DCA FAIL", f"{today_chicago} | {pair}\n{reason}"))
        return {"pair": pair, "status": "failed_kraken"}

    # ── Finalize ──────────────────────────────────────────────
    time.sleep(2)

    try:
        finalize_order(cl_ord_id, order_id, mid=ticker.get("mid"), ohlc_ctx=ohlc_ctx)
    except Exception as e:
        print(f"  {ICONS['WARN']} finalize_order error: {e}")

    try:
        row = sb_get(
            "dca_executions",
            {
                "cl_ord_id": f"eq.{cl_ord_id}",
                "select": "filled_quote_cost,fee_quote,filled_base_volume,avg_price,status",
            },
        )
        if row and isinstance(row, list):
            out = row[0]
            return {"pair": pair, "status": out.get("status") or "placed", "order_id": order_id, **out}
    except Exception as e:
        print(f"  {ICONS['WARN']} Post-finalize DB read failed: {e}")

    return {"pair": pair, "status": "placed", "order_id": order_id}


# ═══════════════════════════════════════════════════════════════
#  RECONCILIATION
# ═══════════════════════════════════════════════════════════════

def try_find_kraken_order(cl_ord_id: str) -> str | None:
    """Try to find a Kraken order by cl_ord_id."""
    try:
        closed = kraken_private("ClosedOrders", {"cl_ordid": cl_ord_id})
        orders = closed.get("closed", {})
        for txid, order in orders.items():
            if order.get("cl_ordid") == cl_ord_id:
                return txid
    except Exception as e:
        print(f"    ClosedOrders search failed: {e}")
    return None


def run_reconciliation(user_id: str):
    """Find stale claimed/placed executions and try to resolve them."""
    print(f"\n{ICONS['RECON']} Reconciliation check...")

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()

    stale = sb_get("dca_executions", {
        "user_id": f"eq.{user_id}",
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
            found = try_find_kraken_order(cl_id)
            if found:
                print("    Found in Kraken! Finalizing...")
                finalize_order(cl_id, found)
            else:
                print("    Not found in Kraken — marking failed")
                sb_update(
                    "dca_executions",
                    {"cl_ord_id": f"eq.{cl_id}"},
                    {
                        "status": "failed_reconciliation",
                        "reason": "Claimed but no Kraken order found after timeout",
                        "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                tg_send(msg_recon(
                    "DCA RECONCILIATION",
                    f"{row['trade_date_chicago']} | {row['pair']}\nClaimed but never placed — marked failed"
                ))

        elif row["status"] == "placed" and order_id:
            try:
                finalize_order(cl_id, order_id)
                print("    Finalized successfully")
            except Exception as e:
                print(f"    Finalize still failing: {e}")
                tg_send(msg_recon(
                    "DCA RECONCILIATION",
                    f"{row['trade_date_chicago']} | {row['pair']}\nPlaced but can't finalize: {e}"
                ))


# ═══════════════════════════════════════════════════════════════
#  WEEKLY SUMMARY
# ═══════════════════════════════════════════════════════════════

def send_weekly_summary(user_id: str):
    """Send Telegram weekly summary (idempotent, Sunday only)."""
    now_chicago = datetime.now(CHICAGO_TZ)

    if now_chicago.weekday() != 6:
        print("Not Sunday — skipping weekly summary")
        return

    week_key = now_chicago.strftime("%G-W%V")

    existing = sb_get("dca_notifications", {
        "notification_type": "eq.weekly_summary",
        "period_key": f"eq.{week_key}",
    })
    if existing:
        print(f"Weekly summary {week_key} already sent")
        return

    week_start = (now_chicago - timedelta(days=7)).strftime("%Y-%m-%d")

    rows = sb_get("dca_executions", {
        "user_id": f"eq.{user_id}",
        "trade_date_chicago": f"gte.{week_start}",
        "select": "pair,status,filled_quote_cost,fee_quote,filled_base_volume,avg_price,mid,trade_date_chicago",
        "order": "trade_date_chicago.asc",
    })

    if not rows:
        print("No executions this week")
        return

    pairs = {}
    for r in rows:
        p = r["pair"]
        if p not in pairs:
            pairs[p] = {
                "filled": 0, "skipped": 0, "failed": 0,
                "total_cost": 0, "total_fee": 0, "total_vol": 0,
                "slippages": [],
            }

        s = pairs[p]
        status = (r.get("status") or "").lower()

        if "filled" in status:
            s["filled"] += 1
            s["total_cost"] += float(r.get("filled_quote_cost") or 0)
            s["total_fee"] += float(r.get("fee_quote") or 0)
            s["total_vol"] += float(r.get("filled_base_volume") or 0)
            mid = float(r.get("mid") or 0)
            avg = float(r.get("avg_price") or 0)
            if mid > 0 and avg > 0:
                s["slippages"].append((avg - mid) / mid * 100)
        elif "skipped" in status:
            s["skipped"] += 1
        elif "failed" in status or "crashed" in status:
            s["failed"] += 1

    lines = [
        f"{ICONS['CHART']} <b>DCA Weekly Summary</b>",
        f"Week: {week_key}",
        "",
    ]

    for pair, s in pairs.items():
        symbol = pair.replace("USD", "")
        all_in = s["total_cost"] + s["total_fee"]
        avg_eff = (all_in / s["total_vol"]) if s["total_vol"] > 0 else 0
        avg_slip = (sum(s["slippages"]) / len(s["slippages"])) if s["slippages"] else 0

        lines.append(f"<b>{symbol}</b>")
        lines.append(
            f"  {ICONS['OK']} {s['filled']} filled"
            + (f" | {ICONS['SKIP']} {s['skipped']} skip" if s["skipped"] else "")
            + (f" | {ICONS['FAIL']} {s['failed']} fail" if s["failed"] else "")
        )
        lines.append(f"  {ICONS['MONEY']} ${all_in:.2f} all-in | {s['total_vol']:.6f} {symbol}")
        if avg_eff > 0:
            lines.append(f"  {ICONS['TREND']} Avg price: ${avg_eff:.6f}")
        if avg_slip != 0:
            lines.append(f"  {ICONS['RULER']} Avg slippage: {avg_slip:.4f}%")
        lines.append("")

    msg = "\n".join(lines)
    tg_send(msg)

    try:
        sb_insert("dca_notifications", {
            "notification_type": "weekly_summary",
            "period_key": week_key,
            "payload": json.dumps({"pairs": list(pairs.keys())}),
        })
    except Exception:
        pass

    print(f"{ICONS['OK']} Weekly summary sent for {week_key}")


# ═══════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    print(f"{ICONS['BOT']} DCA Bot v{VERSION}")

    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    # ── Load settings + user_id (needed for all modes) ────────
    settings_rows = sb_get("dca_settings", {"id": "eq.1"})
    if not settings_rows:
        print(f"{ICONS['FAIL']} No dca_settings found!")
        sys.exit(1)
    settings = settings_rows[0]

    user_id = settings.get("user_id")
    if not user_id:
        print(f"{ICONS['FAIL']} dca_settings.user_id is missing! Run:")
        print("  UPDATE dca_settings SET user_id = '<uuid>' WHERE id = 1;")
        sys.exit(1)

    print(f"   user_id: {user_id[:8]}...")

    # ── Mode dispatch ─────────────────────────────────────────
    if mode == "--reconcile":
        run_reconciliation(user_id)
        return

    if mode == "--weekly":
        send_weekly_summary(user_id)
        return

    # ── Normal run ────────────────────────────────────────────

    # 0) Consume queued SQL commands BEFORE reading settings
    process_dca_commands(DCA_COMMANDS_MAX_PER_RUN)

    # Re-read settings in case commands changed them
    settings_rows = sb_get("dca_settings", {"id": "eq.1"})
    if not settings_rows:
        print(f"{ICONS['FAIL']} No dca_settings found after command processing!")
        sys.exit(1)
    settings = settings_rows[0]

    now_chicago = datetime.now(CHICAGO_TZ)
    now_str = now_chicago.strftime("%Y-%m-%d %H:%M:%S %Z")
    today_chicago = now_chicago.strftime("%Y-%m-%d")

    print(f"{ICONS['CLOCK']} Chicago time: {now_str}")

    # 1) Reconciliation first
    run_reconciliation(user_id)

    # 2) Load enabled orders (filtered by user_id)
    orders = sb_get("dca_orders", {
        "user_id": f"eq.{user_id}",
        "enabled": "eq.true",
        "base_quote_amount": "gt.0",
        "order": "priority.asc",
    })

    if not orders:
        print("   No enabled orders found.")
        return

    print(f"   Found {len(orders)} enabled order(s)")

    # 3) Execute each pair (per-order time window)
    # 3a) Snapshot all unique pairs (regardless of time window)
    seen_pairs = set()
    # Watchlist: extra pairs to snapshot (not traded, just tracking)
    watchlist = ["ETHUSD", "LTCUSD", "BNBUSD", "JUPUSD", "MONUSD", "PEPEUSD", "ASTERUSD", "XMRUSD", "XLMUSD", "SUIUSD", "TONUSD", "CROUSD", "ADAUSD"]
    for wp in watchlist:
        if wp not in seen_pairs:
            seen_pairs.add(wp)
            try:
                t = get_ticker_snapshot(wp)
                save_mid_snapshot(wp, t)
                print(f"   Snapshot {wp}: mid={t['mid']:.6f}")
            except Exception as e:
                print(f"   Snapshot {wp} failed: {e}")
    for o in orders:
        p = o["pair"]
        if p not in seen_pairs:
            seen_pairs.add(p)
            try:
                t = get_ticker_snapshot(p)
                save_mid_snapshot(p, t)
                print(f"   Snapshot {p}: mid={t['mid']:.6f}")
            except Exception as e:
                print(f"   Snapshot {p} failed: {e}")

    results = []
    for order in orders:
        pair = order["pair"]
        o_time = order.get("target_time") or settings.get("target_time", "08:00")
        o_window = int(order.get("time_window_minutes") or settings.get("time_window_minutes", 10))

        o_h, o_m = map(int, o_time.split(":"))
        o_target = now_chicago.replace(hour=o_h, minute=o_m, second=0, microsecond=0)
        o_start = o_target - timedelta(minutes=o_window // 2)
        o_end = o_target + timedelta(minutes=o_window // 2)

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
            tg_send(msg_fail(
                "DCA CRASH",
                f"{today_chicago} | {pair}\nUnhandled: {e}"
            ))
            results.append({"pair": pair, "status": "crashed", "error": str(e)})

        time.sleep(1)

    # 5) Weekly summary (if Sunday)
    send_weekly_summary(user_id)

    # 6) Print summary
    print(f"\n{'='*50}")
    print(f"{ICONS['LIST']} Run complete: {len(results)} pair(s)")
    for r in results:
        print(f"   {r['pair']}: {r['status']}")


if __name__ == "__main__":
    main()
