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

Changelog v1.2 (Phase 2 Step 2 — maker-first execution):
- order_strategy='maker_first': post-only limit leg at best bid, cross-run
  state machine per docs/05-roadmap/dca-phase2-step1-state-machine.md
  (robert-os-hub). Statuses MUST stay in sync with that doc.
- run_maker_inspection: limit_open inspection, deadline cancel-confirm-
  readback, event-level fallback decision (reason marker), dead-letter TTL
  -> manual_required, I6 window+grace bound, I7 budget guard.
- Fallback sized by remaining QUOTE budget (money invariant by construction).
- Reconciliation extended: maker_limit claims also searched in OpenOrders
  (resting limits are invisible to ClosedOrders) and restored to limit_open.
- Dry-run scenario harness via DCA_DRYRUN_MAKER_SCENARIO.
"""

import hashlib
import uuid
import hmac
import base64
import json
import re
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ohlc import build_daily_metrics

VERSION = "1.7.6"

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

# ── Maker-first (Phase 2) constants ──────────────────────────────
# Cron dispatch cadence; the cadence IS the cross-run timer (DP-2).
CRON_CYCLE_MINUTES = int(os.environ.get("DCA_CRON_CYCLE_MINUTES", "5"))
# I6: new orders for a parent event only until window_end + one cycle.
I6_GRACE_MINUTES = CRON_CYCLE_MINUTES
# Dead letter: limit_open may not outlive window_end by more than this.
LIMIT_TTL_MINUTES = int(os.environ.get("DCA_LIMIT_TTL_MINUTES", "60"))
# Dry-run scenario harness (Step 3 evidence window). 'auto' walks
# SCENARIO_SEQUENCE deterministically; an explicit value overrides.
DRYRUN_MAKER_SCENARIO = os.environ.get("DCA_DRYRUN_MAKER_SCENARIO", "auto")

# Reviewer-ordered sequence (2026-07-18): simplest states first, crash
# branches last, so basic breakage surfaces before the emergency paths.
SCENARIO_SEQUENCE = (
    "fill100",                # 1. 100% maker fill
    "nofill",                 # 2. 0% fill -> full fallback
    "partial40",              # 3. partial fill -> remainder fallback
    "reject",                 # 4. post-only reject -> direct fallback
    "crash_after_cancel",     # 5. crash between cancel and fallback
    "crash_after_fb_submit",  # 6. crash between fb submit and DB update
    "repeg_fill",             # 7. bid rises -> re-peg -> maker fill
    "repeg_reject",           # 8. bid meets ask -> re-peg blocked (spread) -> fallback
    "repeg_cap",              # 9. bid above 7D cap -> re-peg blocked -> fallback
)
# ── Cap (veto layer) ───────────────────────────────────────────────
# Two reference modes, selected by dca_settings.cap_mode:
#   'exec_7d' (legacy) — reference = mean of OUR OWN execution mids over 7 days.
#   'ohlc_h7' (v2.3 spec) — reference = H7 SMA of Kraken daily closes, per the
#       project price standard ("H7/H30/H90/H180 = SMA is Kraken daily close",
#       dca-bot-v2.3.md), with the euphoria threshold from the Phase 2 weights
#       matrix and an H90 guard (see cap_decision).
# These defaults keep the legacy behaviour until cap_mode is flipped in the DB.
CAP_MODE_DEFAULT = "exec_7d"
CAP_PCT_DEFAULT = 0.03
CAP_REQUIRE_ABOVE_H90_DEFAULT = False

# Maker-leg statuses whose EVENT still awaits a fallback decision
# (reason IS NULL = decision pending; reason set = event terminal).
PENDING_DECISION_STATUSES = (
    "canceled_partial", "canceled_unfilled", "rejected_postonly",
    "canceled_partial_dry_run", "canceled_unfilled_dry_run",
    "rejected_postonly_dry_run",
)

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


# Reference join window (v2.3 spec §142). A snapshot further than this from the
# fill is not evidence of the market AT the fill, so we say so rather than
# pretend.
#
# SYMMETRIC, which the spec left open -- it says "nearest within 180s" with no
# direction. So the reference may be a snapshot taken up to 180s AFTER the fill,
# and the metric absorbs a little post-trade drift. Backward-only was measured
# on the 33 historical rows and is worse: 6 of them would have no snapshot at
# all and fall back to the ~10-minute-old ticker mid -- a small bias traded for
# a large one. Current split: 26 references before the fill, 7 after, mean
# distance 17.4s.
REF_MID_WINDOW_SECONDS = 180


def resolve_reference_mid(pair: str, fill_ts: datetime, ticker_mid: float | None):
    """The mid that impact_bps / all_in_bps get measured against.

    Returns (ref_mid, mid_source, ref_mid_ts_iso).

    impact_bps is meant to answer "how much worse than the market did we buy",
    which only holds if the reference was observed AT the fill. Before v1.5.0
    this used the T0 ticker mid — sampled when the run started, typically ten
    minutes earlier — so it largely reported market drift instead. The snapshots
    to do this properly have been collected since 2026-02-21; only the join was
    missing.

    Falls back to the run's ticker mid exactly as before when no snapshot is
    close enough, and returns (None, None, None) when there is no mid at all.
    A failed lookup degrades to the fallback — this is telemetry and must never
    be able to block a fill."""
    lo = fill_ts - timedelta(seconds=REF_MID_WINDOW_SECONDS)
    hi = fill_ts + timedelta(seconds=REF_MID_WINDOW_SECONDS)
    try:
        # One filter per key: PostgREST ANDs repeated keys, but urlencode would
        # need a list to emit them and the semantics are easy to get wrong. A
        # lower bound plus ascending order is unambiguous, and the window is
        # narrower than the limit can truncate — snapshots are written once per
        # cron cycle, so 360s holds a handful of rows, never 50.
        rows = sb_get("dca_mid_snapshots", {
            "pair": f"eq.{pair}",
            "ts": f"gte.{lo.isoformat()}",
            "order": "ts.asc",
            "limit": "50",
            "select": "ts,mid",
        }) or []
    except Exception as e:
        print(f"  {ICONS['WARN']} snapshot lookup failed: {e} — using ticker mid")
        rows = []

    best = None
    for r in rows:
        if r.get("mid") is None or not r.get("ts"):
            continue
        try:
            ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts > hi:
            break
        delta = abs((ts - fill_ts).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, float(r["mid"]), ts)

    if best is not None:
        print(f"  ref mid: ${best[1]:.6f} from snapshot {best[0]:.1f}s from fill")
        return best[1], "snapshot", best[2].isoformat()
    if ticker_mid is not None and ticker_mid > 0:
        print(f"  ref mid: ${float(ticker_mid):.6f} from run ticker (no snapshot within {REF_MID_WINDOW_SECONDS}s)")
        return float(ticker_mid), "ticker_fallback", None
    return None, None, None


def fill_timestamp(order_data: dict, observed_at: datetime) -> datetime:
    """When the order ACTUALLY closed, per Kraken's own `closetm`.

    `execution_finished_at` is when a later cron cycle noticed the fill, which
    since the maker-first cutover can be minutes afterwards. Measured over the
    33 rows carrying bps: lag averages 17.7s but reaches 151.4s -- 84% of
    REF_MID_WINDOW_SECONDS, the half-width of THIS join's window and no other --
    spent on our own polling, which would drag the window most of the way off
    the event it describes."""
    try:
        closetm = order_data.get("closetm")
        if closetm:
            return datetime.fromtimestamp(float(closetm), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    return observed_at


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


_OHLC_CACHE: dict = {}


def get_ohlc_ctx(pair: str, preloaded: dict | None = None) -> dict:
    """Daily OHLC metrics for `pair`, fetched at most once per run.

    The T0 path already builds this for Phase 1.5 telemetry and hands it in;
    the fallback and re-peg paths run in later cron cycles (fresh processes)
    and fetch their own. Returns {} when Kraken OHLC is unavailable."""
    if preloaded:
        _OHLC_CACHE[pair] = preloaded
        return preloaded
    if pair in _OHLC_CACHE:
        return _OHLC_CACHE[pair]
    try:
        ctx = build_daily_metrics(pair, days=220)
    except Exception as e:
        print(f"  {ICONS['WARN']} OHLC fetch for cap failed: {e}")
        ctx = {}
    _OHLC_CACHE[pair] = ctx
    return ctx


def get_cap_context(pair: str, user_id: str, settings: dict, ohlc_ctx: dict | None = None):
    """Resolve the cap reference for `pair`. Returns (ref_price, h90, label).

    ref_price None means NO cap check runs at all — a missing reference has
    always meant "proceed" here (DP-4: the day must never end unbought while
    funds exist), and that stays true if Kraken OHLC is unreachable."""
    mode = settings.get("cap_mode") or CAP_MODE_DEFAULT
    if mode == "ohlc_h7":
        ctx = get_ohlc_ctx(pair, ohlc_ctx)
        return ctx.get("H7"), ctx.get("H90"), "H7"
    return get_7d_ref_price(pair, user_id), None, "7D"


def cap_params(settings: dict):
    """(cap_pct, require_above_h90) with legacy-safe defaults."""
    raw_pct = settings.get("cap_pct")
    try:
        cap_pct = CAP_PCT_DEFAULT if raw_pct is None else float(raw_pct)
    except (TypeError, ValueError):
        cap_pct = CAP_PCT_DEFAULT
    raw_req = settings.get("cap_require_above_h90")
    require = CAP_REQUIRE_ABOVE_H90_DEFAULT if raw_req is None else bool(raw_req)
    return cap_pct, require


def cap_telemetry(ref_price, cap_pct, h90) -> dict:
    """The two numbers that make a cap decision reconstructible from the row.

    `h7` and `mid` are stored earlier in the SAME run; these add the 90-day
    floor the guard reads and the threshold actually applied, so afterwards the
    decision is checkable by arithmetic instead of by parsing the reason text:
        skip <=> mid > cap_price AND (guard off OR mid > h90)
    and, in ohlc_h7 mode, cap_price / h7 - 1 == cap_pct.
    Both NULL means no reference was available and NO cap check ran."""
    return {
        "h90": h90,
        "cap_price": ref_price * (1.0 + cap_pct) if ref_price else None,
    }


def cap_decision(price, ref_price, h90, cap_pct, require_above_h90):
    """PURE veto decision — shared by the T0 check, the DP-5 fallback re-check
    and the re-peg guard, so all three can never drift apart.

    Skip only when the price is stretched against the SHORT reference and --
    when the guard is on -- also above the 90-day trend. The H90 condition
    closes a structural blind spot of any 7-day mean: straight after a crash a
    violent bounce reads as far above H7 while still sitting well BELOW H90,
    i.e. exactly the cheap day an accumulator wants to buy. Measured on 500d of
    KAS daily closes: of the 4 days above H7 x 1.20, three were below H90.

    Returns (skip: bool, detail: str | None)."""
    if price is None or ref_price is None or ref_price <= 0:
        return (False, None)
    cap_price = ref_price * (1.0 + cap_pct)
    if price <= cap_price:
        return (False, None)
    if require_above_h90 and (h90 is None or price <= h90):
        return (False, None)
    pct_over = ((price / ref_price) - 1.0) * 100.0
    return (True, f"+{pct_over:.2f}% vs ref (cap ${cap_price:.6f})")

#  TELEGRAM
# ═══════════════════════════════════════════════════════════════

# Formatting markers. NOT "<b>", deliberately.
#
# The old scheme escaped the whole message and then un-escaped `&lt;b&gt;` back
# into `<b>`, which restores the tag WHEREVER it appears -- including inside
# text the bot did not write. That was harmless while every message was built
# from our own words. It stopped being harmless the moment Kraken's untouched
# reply started being shown verbatim: an error string containing markup would
# have had it rendered rather than displayed.
#
# These two characters cannot occur in a Kraken error or a pair name, so a tag
# can only come from a formatter below -- the allowlist is about ORIGIN, which
# is what it was always meant to be, rather than about spelling.
B_ON, B_OFF = "\x00b\x01", "\x00/b\x01"
CODE_ON, CODE_OFF = "\x00code\x01", "\x00/code\x01"

_TG_TAGS = {
    B_ON: "<b>", B_OFF: "</b>",
    CODE_ON: "<code>", CODE_OFF: "</code>",
}


def _tg_html(text: str) -> str:
    """Escape everything, then turn our own markers into tags. Order matters:
    escaping first means nothing in the content can become a tag afterwards."""
    t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for marker, tag in _TG_TAGS.items():
        t = t.replace(marker, tag)
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
    return f"{ICONS['OK']} {B_ON}{title}{B_OFF}\n{body}"


def msg_warn(title: str, body: str) -> str:
    return f"{ICONS['WARN']} {B_ON}{title}{B_OFF}\n{body}"


def msg_fail(title: str, body: str) -> str:
    return f"{ICONS['FAIL']} {B_ON}{title}{B_OFF}\n{body}"


def msg_recon(title: str, body: str) -> str:
    return f"{ICONS['RECON']} {B_ON}{title}{B_OFF}\n{body}"


def msg_dryrun(title: str, body: str) -> str:
    return f"{ICONS['DRYRUN']} {B_ON}{title}{B_OFF}\n{body}"


def pair_label(pair: str) -> str:
    """KASUSD -> KAS/USD. DISPLAY ONLY — never hand this back to Kraken."""
    for quote in ("USDT", "USDC", "USD", "EUR"):
        if pair.endswith(quote) and len(pair) > len(quote):
            return f"{pair[:-len(quote)]}/{quote}"
    return pair


# ── Failure messages: one line to read, one block to debug ───────────
#
# A failure notification serves two people who are the same person in different
# moods. Reading the phone, you want to know what happened in one line. Fixing
# it afterwards, you want Kraken's exact words -- and a paraphrase is useless
# there, because the string you search for has to be the string Kraken sent.
# Previously the raw text was pasted into the sentence, so it was bad at both:
# unreadable at a glance and buried when it mattered.
#
# So: a sentence, then the untouched original in its own monospace block.
# Nothing is dropped -- if a code has no translation, the sentence says to read
# the block rather than inventing a cause.

KRAKEN_ERROR_LT = {
    "eorder:insufficient funds":     "nepakanka lėšų",
    "eorder:order minimum not met":  "suma mažesnė už minimalų orderį",
    "eorder:invalid price":          "netinkama kaina",
    "eorder:invalid volume":         "netinkamas kiekis",
    "eorder:rate limit exceeded":    "viršytas orderių limitas",
    "eapi:rate limit exceeded":      "viršytas užklausų limitas",
    "eapi:invalid nonce":            "netinkamas nonce (raktas naudojamas lygiagrečiai)",
    "eapi:invalid key":              "netinkamas API raktas",
    "egeneral:permission denied":    "raktui trūksta leidimų",
    "egeneral:temporary lockout":    "laikinas Kraken užraktas",
    "egeneral:invalid arguments":    "netinkami užklausos parametrai",
    "eservice:unavailable":          "Kraken paslauga neprieinama",
    "eservice:busy":                 "Kraken serveris užimtas",
    "eorder:cannot open position":   "pozicijos atidaryti negalima",
    "eorder:post only order":        "post-only orderis atmestas",
}

# Anchored on the real Kraken error CLASSES rather than "E<word>:". The loose
# pattern matched "failed:" inside our own wrapper sentence once the match
# was made case-insensitive, and reported "ed:" as the error code.
_KRAKEN_CODE_RE = re.compile(
    r"E(?:General|API|Order|Service|Query|Funding|Trade|Database|Session|Auth)"
    r":[A-Za-z0-9 _\-\.]+",
    re.IGNORECASE)


def kraken_error_code(error) -> str | None:
    """PURE. The bare `EClass:Message` Kraken sent, out of whatever wraps it.

    The stored reason is a sentence with a Python list inside it, e.g.
    `limit AddOrder failed: ['EOrder:Insufficient funds']`. The code is the
    only part worth putting in a debug block; the rest is our own wording.
    Returns None when nothing matches, and then the ORIGINAL text is shown
    untouched -- guessing at an unrecognised failure is how a message ends up
    lying about what went wrong.
    """
    m = _KRAKEN_CODE_RE.search(str(error or ""))
    return m.group(0).strip().rstrip("'\"]") if m else None


def kraken_error_lt(error) -> str | None:
    """PURE. Lithuanian cause, or None when we do not have one for this code."""
    code = kraken_error_code(error)
    if not code:
        return None
    return KRAKEN_ERROR_LT.get(code.lower())


def msg_exec_fail(trade_date: str, pair: str, action: str, error) -> str:
    """The failure notification: sentence first, Kraken's own words below."""
    cause = kraken_error_lt(error)
    code = kraken_error_code(error) or str(error or "").strip() or "(be teksto)"
    lines = [
        f"{ICONS['FAIL']} {B_ON}DCA KLAIDA{B_OFF}",
        f"{trade_date} | {pair_label(pair)}",
        action,
        f"Priežastis: {cause}" if cause else "Priežastis: žr. Kraken atsakymą žemiau.",
        "",
        "Kraken atsakymas:",
        f"{CODE_ON}{code}{CODE_OFF}",
    ]
    return "\n".join(lines)


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

def check_balance_usd() -> tuple[float, float, str]:
    """SPENDABLE USD on Kraken. Returns (available, held, source).

    The docstring used to say "available" while the call said `Balance`, which
    is the TOTAL -- it includes USD already committed to resting orders. On
    2026-07-30 that lie cost a day: the preflight saw enough, waved the order
    through, and Kraken refused it with EOrder:Insufficient funds because the
    money was locked in an open order. A preflight that cannot fail before the
    exchange does is not a preflight.

    `BalanceEx` returns `balance` and `hold_trade` per asset, so
    available = balance - hold_trade (docs.kraken.com, POST /private/BalanceEx).

    Falls back to `Balance` rather than blocking the buy: BalanceEx may need a
    permission this key does not have, and a funding check must never be the
    thing that stops trading. The fallback returns the OLD, optimistic number,
    so the source is returned with it and gets printed -- a degraded check that
    looks identical to a good one is how the first version of this went wrong.
    """
    try:
        ex = kraken_private("BalanceEx")
        row = ex.get("ZUSD") or {}
        if "balance" in row:
            total = float(row.get("balance") or 0)
            held = float(row.get("hold_trade") or 0)
            return (max(total - held, 0.0), held, "BalanceEx")
    except Exception as e:
        # Deliberately broad: a permission error, an unknown endpoint and a
        # dropped connection must all degrade to the previous behaviour rather
        # than stop a buy. Only the funding CHECK is weakened, never the buy.
        print(f"  {ICONS['WARN']} BalanceEx unavailable ({e}) -- falling back to Balance (total, not spendable)")
    balances = kraken_private("Balance")
    return (float(balances.get("ZUSD", 0)), 0.0, "Balance")


# Days of remaining buys below which we warn. Notification only -- it never
# changes a trading decision -- so it defaults ON even without the migration.
LOW_BALANCE_WARN_DAYS_DEFAULT = 5


def warn_if_low_balance(usd_balance: float, daily_burn: float, settings: dict) -> None:
    """Telegram warning while the Kraken USD balance can still be topped up.

    A funding gap does not fail loudly: the day is simply skipped, and a
    skipped day is never bought back because there is no carryover. That is
    how 2026-07-23 was lost. This fires while there is still time to act.

    Fires at most once per day: the balance preflight sits AFTER the claim
    insert, which is day-unique, so later runs inside the same window return
    at the 409 long before reaching here."""
    raw = settings.get("low_balance_warn_days")
    try:
        threshold = LOW_BALANCE_WARN_DAYS_DEFAULT if raw is None else float(raw)
    except (TypeError, ValueError):
        threshold = LOW_BALANCE_WARN_DAYS_DEFAULT
    if threshold <= 0 or daily_burn <= 0:
        return
    days_left = usd_balance / daily_burn
    if days_left >= threshold:
        return
    print(f"  {ICONS['WARN']} Low balance: ${usd_balance:.2f} = ~{days_left:.1f} days of buys")

    # ESCALATION, because the old message read identically at 4.9 days and at
    # 0.2 days. A fraction below one is not "running low", it is a scheduled
    # failure with a time on it, and it has to be said that way or it gets
    # skimmed like every other warning that cried the same wolf yesterday.
    if days_left < 1:
        headline = "RYTOJ DCA PIRKIMAS NEPAVYKS"
        tail = (f"Reikia bent ${daily_burn:.2f} Kraken sąskaitoje iki rytojaus ryto.\n"
                f"Praleisti pirkimai atgaline data NEVYKDOMI.")
    else:
        headline = "Dėmesio: senka DCA lėšų likutis"
        tail = "Prašome papildyti sąskaitą. Praleisti DCA pirkimai atgaline data nevykdomi."

    tg_send(msg_warn(
        headline,
        f"\n"
        f"• Kraken USD: ${usd_balance:.2f}\n"
        f"• Dienos biudžetas: ${daily_burn:.2f}\n"
        f"• Likutis pirkimams: ~{days_left:.1f} d. (riba – {threshold:.0f} d.)\n"
        f"\n"
        f"{tail}"
    ))


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


def _buys_word(n: int) -> str:
    """Lithuanian plural for `pirkimas`. 1 -> pirkimas, 2-9 -> pirkimai, else pirkimų."""
    if n % 10 == 1 and n % 100 != 11:
        return "pirkimas"
    if 2 <= n % 10 <= 9 and not 11 <= n % 100 <= 19:
        return "pirkimai"
    return "pirkimų"


def _remaining_after_buy_line() -> str:
    """What is ACTUALLY left, read after the fill, for the fill notification.

    This is where the low-balance number belongs. The preflight used to report
    it four minutes BEFORE the buy, so it named the money about to be spent --
    on 2026-08-03 it said $11.66 while the answer that mattered was $1.66.

    The count is the part worth reading: "$1.66 (0 pirkimų)" says the next buy
    fails, which "$1.66" alone does not.

    RETURNS "" ON ANY FAILURE. A fill notification must never depend on a second
    API call succeeding -- the buy already happened and the message about it is
    more important than the extra line.
    """
    try:
        avail, _held, _src = check_balance_usd()
        rows = sb_get("dca_orders", {"enabled": "eq.true",
                                     "select": "base_quote_amount,bonus_quote_amount"})
        burn = sum(float(r.get("base_quote_amount") or 0)
                   + float(r.get("bonus_quote_amount") or 0) for r in (rows or []))
        if burn <= 0:
            return f"\nLiko:   ${avail:.2f}"
        n = int(avail // burn)
        return f"\nLiko:   ${avail:.2f}  ({n} {_buys_word(n)})"
    except Exception as e:
        print(f"  {ICONS['WARN']} remaining-balance line skipped: {e}")
        return ""


def finalize_order(cl_ord_id: str, order_id: str, mid: float | None = None, ohlc_ctx: dict | None = None, label: str = "FILLED"):
    """Query Kraken for fill details and update DB."""
    if not order_id:
        return

    # Terminal guard (best-effort): a terminal row is final — never rewrite
    # it, never re-send its Telegram message. Overlapping runs and repeated
    # reconciliation both funnel through here, so this is the single choke
    # point for finalize idempotency.
    # The OHLC columns ride along on the same read: if T0 already stored them,
    # this run must NOT refetch, because a second reading of H7 taken minutes
    # later no longer reproduces the cap_price derived from the first.
    stored_ohlc: dict = {}
    try:
        cur = sb_get("dca_executions",
                     {"cl_ord_id": f"eq.{cl_ord_id}", "select": "status,h7,h30,h90"})
        row0 = cur[0] if cur else {}
        cur_status = row0.get("status")
        if row0.get("h7") is not None:
            stored_ohlc = {
                name: float(row0[col])
                for name, col in (("H7", "h7"), ("H30", "h30"), ("H90", "h90"))
                if row0.get(col) is not None
            }
        if cur_status is not None and cur_status not in ("claimed", "placed", "limit_open"):
            print(f"  finalize skip: {cl_ord_id} already terminal ({cur_status})")
            return
    except Exception as e:
        print(f"  {ICONS['WARN']} finalize status pre-check failed: {e} — proceeding")

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

    # ── pair ─────────────────────────────────────────────────
    # cl_ord_id carries OUR config's pair string; Kraken's descr.pair can use
    # its own aliases (XXBTZUSD vs XBTUSD), so it is only the fallback here.
    pair = _pair_from_cl_ord_id(cl_ord_id)
    if pair == "?":
        try:
            pair = (order_data.get("descr") or {}).get("pair") or "?"
        except Exception:
            pair = "?"

    # ── bps metrics (Phase 1.5, reference join v1.5.0) ───────
    # Measured against the market AT the fill, not at T0. `mid` (the run's
    # ticker mid) is now only the fallback; see resolve_reference_mid.
    impact_bps = None
    all_in_bps = None
    ref_mid = None
    mid_source = None
    ref_mid_ts = None
    if vol_exec > 0 and avg_px > 0 and pair != "?":
        ref_mid, mid_source, ref_mid_ts = resolve_reference_mid(
            pair, fill_timestamp(order_data, finished_at_utc), mid)
    elif mid is not None and mid > 0 and vol_exec > 0 and avg_px > 0:
        # No pair to look a snapshot up by — keep the pre-v1.5.0 behaviour
        # rather than dropping the metric entirely.
        ref_mid, mid_source = float(mid), "ticker_fallback"
    if ref_mid and ref_mid > 0 and vol_exec > 0 and avg_px > 0:
        impact_bps = round(((avg_px / ref_mid) - 1) * 10_000, 4)
        all_in_price = (cost + fee) / vol_exec
        all_in_bps = round(((all_in_price / ref_mid) - 1) * 10_000, 4)
        print(f"  impact_bps: {impact_bps:.4f} | all_in_bps: {all_in_bps:.4f} | mid_source: {mid_source}")

    # ── market context: written ONCE, at T0 ──────────────────
    # T0 stores H7/H30 alongside the cap_price it derives from them, so a fill
    # observed by a later cron cycle must reuse the row rather than refetch.
    # Refetching was the v1.4.2 behaviour and it silently desynced the row:
    # on 2026-07-28 the T0 anchor was 0.02818714 and the 07:03 refetch wrote
    # 0.02818143, so cap_price / h7 - 1 read 20.02% for a 20% cap.
    ohlc_cols: dict = {}
    if stored_ohlc:
        ohlc_ctx = stored_ohlc
    else:
        # No T0 context on the row — a pre-v1.4.4 row, or a path that never ran
        # the cap check. get_ohlc_ctx caches per run and returns {} if Kraken
        # OHLC is unavailable, writing NULL: telemetry never blocks a fill.
        if ohlc_ctx is None and pair != "?":
            ohlc_ctx = get_ohlc_ctx(pair)
        ohlc_cols = {
            "h7": ohlc_ctx.get("H7") if ohlc_ctx else None,
            "h30": ohlc_ctx.get("H30") if ohlc_ctx else None,
            "ohlc_ts": datetime.now(timezone.utc).isoformat() if ohlc_ctx else None,
        }

    # MERGE raw, never replace. `raw` is shared state: the re-peg path keeps its
    # counter and history there, and v1.7.2 records the preflight balance
    # reading there. Replacing it wholesale on the fill destroys both -- and it
    # did, on the very first live run of the fix whose whole purpose was to make
    # that reading outlive the run. Third time this trap has been walked into,
    # so it is now closed at the write rather than remembered at each caller.
    prior_raw = {}
    try:
        prior = sb_get("dca_executions", {"cl_ord_id": f"eq.{cl_ord_id}", "select": "raw"})
        if prior:
            loaded = _safe_json_load(prior[0].get("raw"))
            if isinstance(loaded, dict):
                prior_raw = loaded
    except Exception as e:
        print(f"  {ICONS['WARN']} raw merge lookup failed: {e}")
    merged_raw = {**prior_raw, **(order_data if isinstance(order_data, dict) else {})}

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
            "raw": json.dumps(merged_raw),
            "impact_bps": impact_bps,
            "all_in_bps": all_in_bps,
            "mid_source": mid_source,
            "ref_mid": ref_mid,
            "ref_mid_ts": ref_mid_ts,
            **ohlc_cols,
        },
    )

    if TG_NOTIFY_ON_FILL:
        filled_ts = finished_at_utc.astimezone(CHICAGO_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

        # Market context is its own block, separated from the settlement
        # figures by a blank line: the numbers above are what was paid, the
        # numbers below are what the market looked like. Every line is dropped
        # when its data is missing — no placeholders, no "unknown".
        market_lines = []

        bps_parts = []
        if impact_bps is not None:
            sign = "+" if float(impact_bps) >= 0 else ""
            bps_parts.append(f"Impact: {sign}{float(impact_bps):.1f} bps")
        if all_in_bps is not None:
            sign = "+" if float(all_in_bps) >= 0 else ""
            bps_parts.append(f"All-in: {sign}{float(all_in_bps):.1f} bps")
        if bps_parts:
            market_lines.append(" | ".join(bps_parts))

        # H7 is the cap anchor and H90 the guard, so both belong here now that
        # the veto reads them. H30 carries no decision weight -- it is kept
        # because the ORDER of the three (H7 < H30 < H90 = drifting down) reads
        # the trend at a glance, which no single number does. Same precision as
        # the Price line above so they compare directly.
        if ohlc_ctx:
            parts = [
                f"{name}: ${float(ohlc_ctx[name]):.5f}"
                for name in ("H7", "H30", "H90")
                if ohlc_ctx.get(name) is not None
            ]
            if parts:
                market_lines.append(" | ".join(parts))

        # The reference the two bps figures above were measured against. It is
        # normally the market AT the fill, so it needs no qualifier; when no
        # snapshot was within 180s we fell back to the price seen when the run
        # STARTED, and that is worth saying, because it makes the bps a weaker
        # claim. Qualified in plain words, not with the enum. No mid, no line.
        if ref_mid is not None and float(ref_mid) > 0:
            qualifier = " (run ticker)" if mid_source == "ticker_fallback" else ""
            market_lines.append(f"Mid: ${float(ref_mid):.5f}{qualifier}")

        market_block = "\n\n" + "\n".join(market_lines) if market_lines else ""
        symbol = pair.replace("USD", "")
        tg_send(msg_ok(
            f"DCA {pair_label(pair)} {label}",
            f"{filled_ts}\n\n"
            f"Amount: {vol_exec} {symbol}\n"
            f"Price:  ${avg_px:.5f}\n"
            f"Cost:   ${cost:.4f}\n"
            f"Fee:    ${fee:.4f}\n"
            f"Total:  ${all_in:.4f}"
            f"{_remaining_after_buy_line()}"
            f"{market_block}"
        ))


# ═══════════════════════════════════════════════════════════════
#  MAKER-FIRST STATE MACHINE (Phase 2)
#  Authoritative spec: robert-os-hub docs/05-roadmap/
#  dca-phase2-step1-state-machine.md — no states beyond that doc.
# ═══════════════════════════════════════════════════════════════

def _pick_dry_scenario(user_id: str) -> str:
    """Deterministic scenario progression for the Step 3 evidence window.
    A scenario is consumed once any maker dry event has recorded it in raw;
    the next claim takes the first unconsumed one from SCENARIO_SEQUENCE.
    DCA_DRYRUN_MAKER_SCENARIO != 'auto' overrides (e.g., to repeat one)."""
    if DRYRUN_MAKER_SCENARIO != "auto":
        return DRYRUN_MAKER_SCENARIO
    consumed = set()
    try:
        rows = sb_get("dca_executions", {
            "user_id": f"eq.{user_id}",
            "attempt_type": "eq.maker_limit",
            "cl_ord_id": "like.*-dry",
            "select": "raw",
        }) or []
        for r in rows:
            raw = _safe_json_load(r.get("raw")) or {}
            sc = raw.get("maker_scenario")
            if sc:
                consumed.add(sc)
    except Exception as e:
        print(f"  {ICONS['WARN']} scenario progress query failed: {e}")
    for sc in SCENARIO_SEQUENCE:
        if sc not in consumed:
            return sc
    print("  All dry-run scenarios consumed — defaulting to fill100")
    return "fill100"


def _window_bounds_for(trade_date: str, target_time: str, window_minutes: int):
    """Chicago window (start, end) for a given trade date and target time."""
    d = datetime.strptime(trade_date, "%Y-%m-%d")
    h, m = map(int, target_time.split(":"))
    target = datetime(d.year, d.month, d.day, h, m, tzinfo=CHICAGO_TZ)
    half = timedelta(minutes=window_minutes // 2)
    return target - half, target + half


def _load_order_row(dca_order_id):
    if dca_order_id is None:
        return None
    try:
        rows = sb_get("dca_orders", {"id": f"eq.{dca_order_id}"})
        return rows[0] if rows else None
    except Exception as e:
        print(f"  {ICONS['WARN']} dca_orders load failed id={dca_order_id}: {e}")
        return None


def _limit_window_end(row: dict, settings: dict):
    """Window end (Chicago) for a maker-leg row.

    Derived from the row's dca_order (strategy unit). If the order row is
    gone, fall back to execution_started_at + window so TTL/I6 still bound
    the row instead of leaving it immortal."""
    order = _load_order_row(row.get("dca_order_id"))
    if order and order.get("target_time"):
        w = int(order.get("time_window_minutes") or settings.get("time_window_minutes", 10))
        _, end = _window_bounds_for(row["trade_date_chicago"], order["target_time"], w)
        return end
    started = datetime.fromisoformat(str(row["execution_started_at"]).replace("Z", "+00:00"))
    w = int(settings.get("time_window_minutes", 10))
    return started.astimezone(CHICAGO_TZ) + timedelta(minutes=w)


def _mark_manual_required(row: dict, note: str):
    """Dead letter (4.1): automation stops for this event, human takes over."""
    sb_update("dca_executions", {"cl_ord_id": f"eq.{row['cl_ord_id']}"}, {
        "status": "manual_required",
        "reason": note,
        "execution_finished_at": _now_utc_iso(),
    })
    tg_send(msg_fail(
        "DCA MANUAL REQUIRED",
        f"{row['trade_date_chicago']} | {row['pair']}\n"
        f"order_id: {row.get('order_id') or '?'}\n{note}\n"
        f"Automatika sustabdyta siam ivykiui. Patikrink orderi Kraken'e ranka."
    ))


def _find_open_kraken_order(cl_ord_id: str) -> str | None:
    """Find a RESTING order by cl_ord_id. ClosedOrders can't see these —
    without this, a crash between limit AddOrder and the DB update would
    orphan an open order and reconciliation would wrongly mark it failed."""
    try:
        oo = kraken_private("OpenOrders")
        for txid, order in (oo.get("open") or {}).items():
            if order.get("cl_ordid") == cl_ord_id:
                return txid
    except Exception as e:
        print(f"    OpenOrders search failed: {e}")
    return None


def _cancel_confirm_readback(row: dict) -> str | None:
    """Deadline steps 1-4: cancel -> confirm -> READBACK (sole authority)
    -> write canceled_* / filled. Returns 'filled', 'canceled', or None
    (not confirmed yet — row stays limit_open, TTL bounds the retries)."""
    cl = row["cl_ord_id"]
    oid = row["order_id"]

    try:
        kraken_private("CancelOrder", {"txid": oid})
        print(f"    cancel sent: {oid}")
    except KrakenError as e:
        # Canceling an already closed/canceled order errors — that is fine,
        # the readback below decides. Idempotent by design.
        print(f"    cancel error (may already be closed/canceled): {e}")

    try:
        od = kraken_private("QueryOrders", {"txid": oid})
    except Exception as e:
        print(f"    readback query failed: {e}")
        return None
    o = od.get(oid)
    if not o:
        return None

    st = o.get("status", "")
    if st == "closed":
        # Scenario #8 (cancel/fill race): filled 100% before the cancel
        # landed. Readback wins — this is a normal maker fill, no fallback.
        print(f"    race: order closed 100% before cancel — maker fill")
        finalize_order(cl, oid, mid=row.get("mid"))
        return "filled"
    if st not in ("canceled", "expired"):
        print(f"    cancel not confirmed yet (status={st})")
        return None

    vol_exec = float(o.get("vol_exec", 0) or 0)
    cost = float(o.get("cost", 0) or 0)
    fee = float(o.get("fee", 0) or 0)
    price = float(o.get("price", 0) or 0)
    new_status = "canceled_partial" if vol_exec > 0 else "canceled_unfilled"
    print(f"    readback: vol_exec={vol_exec} cost=${cost} fee=${fee} -> {new_status}")

    updates = {
        "status": new_status,
        "execution_finished_at": _now_utc_iso(),
        "raw": json.dumps(o),
    }
    if vol_exec > 0:
        updates.update({
            "filled_quote_cost": cost,
            "fee_quote": fee,
            "filled_base_volume": vol_exec,
            "avg_price": price if price > 0 else None,
        })
    # reason stays NULL: the fallback decision (event resolution) sets it.
    sb_update("dca_executions", {"cl_ord_id": f"eq.{cl}"}, updates)
    return "canceled"


def _fallback_decision(row: dict, settings: dict, user_id: str, window_end, dry_run: bool, scenario: str | None):
    """Event-level resolution after the maker leg reached a decision-pending
    state (canceled_* / rejected_postonly, reason IS NULL). Exactly one
    outcome, recorded in the maker row's `reason` — that makes the EVENT
    terminal and auditable:
      fallback_created | fallback_blocked_i6 | fallback_none_budget |
      fallback_skipped_above_cap | fallback_below_ordermin |
      fallback_failed_kraken
    Transient errors (ticker/pair info) leave reason NULL — retried next
    run, bounded by I6."""
    pair = row["pair"]
    cl_base = row["cl_ord_id"]
    trade_date = row["trade_date_chicago"]
    total_target = float(row["requested_quote_amount_base"])
    spent = float(row.get("filled_quote_cost") or 0) + float(row.get("fee_quote") or 0)

    def mark(reason: str):
        sb_update("dca_executions", {"cl_ord_id": f"eq.{cl_base}"}, {"reason": reason})

    now_chi = datetime.now(CHICAGO_TZ)

    # I6: new orders for this parent event only until window_end + grace.
    if now_chi > window_end + timedelta(minutes=I6_GRACE_MINUTES):
        mark("fallback_blocked_i6")
        tg_send(msg_warn(
            "DCA PARTIAL",
            f"{trade_date} | {pair}\n"
            f"Maker lega baigesi uz I6 ribos — fallback nekuriamas.\n"
            f"Ivykdyta: ${spent:.4f} is ${total_target:.2f}"
        ))
        return

    # I7 guard: never even compute a fallback on a dead budget.
    b_rem = total_target - spent
    if b_rem <= 0:
        mark("fallback_none_budget")
        return

    try:
        ticker = get_ticker_snapshot(pair)
    except Exception as e:
        print(f"    fallback ticker failed: {e} — retry next run")
        return

    # DP-5: cap re-checked with the CURRENT mid, not the T0 one.
    if ticker.get("mid"):
        cap_pct, require_h90 = cap_params(settings)
        ref_price, h90, label = get_cap_context(pair, user_id, settings)
        skip, detail = cap_decision(ticker["mid"], ref_price, h90, cap_pct, require_h90)
        if skip:
            mark("fallback_skipped_above_cap")
            tg_send(msg_warn(
                "DCA PARTIAL",
                f"{trade_date} | {pair}\n"
                f"Fallback skip: mid {detail} virs {label} cap.\n"
                f"Ivykdyta: ${spent:.4f} is ${total_target:.2f}"
            ))
            return

    if not ticker.get("ask") or ticker["ask"] <= 0:
        print("    fallback: no valid ask — retry next run")
        return

    try:
        pair_info = get_asset_pair_info(pair)
    except Exception as e:
        print(f"    fallback pair info failed: {e} — retry next run")
        return

    taker_rate = max(float(settings.get("taker_fee_rate") or 0), 0.0)
    safe_total = max(b_rem - USD_SAFETY_MARGIN, 0.0)
    cost_target = safe_total / (1.0 + taker_rate)
    vol = floor_to_decimals(cost_target / ticker["ask"], pair_info["lot_decimals"])

    if vol <= 0 or vol < pair_info["ordermin"]:
        mark("fallback_below_ordermin")
        tg_send(msg_warn(
            "DCA PARTIAL",
            f"{trade_date} | {pair}\n"
            f"Likutis ${b_rem:.4f} < min orderis — diena priimta daline apimtimi.\n"
            f"Ivykdyta: ${spent:.4f} is ${total_target:.2f}"
        ))
        return

    # Claim-first (I3): the fallback leg gets its own row BEFORE AddOrder.
    fb_cl = cl_base + "-fb"
    fb_row = {
        "user_id": user_id,
        "trade_date_chicago": trade_date,
        "pair": pair,
        "cl_ord_id": fb_cl,
        "status": "claimed",
        "requested_quote_amount_base": round(b_rem, 6),
        "execution_started_at": _now_utc_iso(),
        "parent_event_id": row.get("parent_event_id"),
        "attempt_type": "maker_fallback",
        "dca_order_id": row.get("dca_order_id"),
    }
    est_cost = vol * ticker["ask"]
    est_fee = est_cost * taker_rate
    if dry_run:
        fb_row["raw"] = json.dumps({
            "dry_run": True,
            "maker_scenario": scenario,
            "base_volume": vol,
            "ask": ticker["ask"],
            "estimated_cost": round(est_cost, 6),
            "estimated_fee": round(est_fee, 6),
        })
    try:
        sb_insert("dca_executions", fb_row)
        print(f"    fallback claimed: {fb_cl} (B_rem=${b_rem:.4f}, vol={vol})")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # Another run already created the fallback leg — its own
            # machinery (placed/stale rec) finishes it. Event is resolved.
            mark("fallback_created")
            return
        raise

    def update_fb(updates: dict):
        sb_update("dca_executions", {"cl_ord_id": f"eq.{fb_cl}"}, updates)

    update_fb({
        "bid": ticker["bid"], "ask": ticker["ask"], "mid": ticker["mid"],
        "mid_ts": _now_utc_iso(),
    })

    if dry_run:
        if scenario == "crash_after_fb_submit":
            # Simulated crash: leg stays 'claimed'; stale reconciliation
            # must finalize it WITHOUT re-buying (scenario #6).
            mark("fallback_created")
            print("    DRYRUN simulated crash after fb submit — left claimed")
            return
        update_fb({
            "status": "filled_dry_run",
            "filled_quote_cost": round(est_cost, 6),
            "fee_quote": round(est_fee, 6),
            "filled_base_volume": vol,
            "avg_price": ticker["ask"],
            "execution_finished_at": _now_utc_iso(),
        })
        mark("fallback_created")
        tg_send(msg_dryrun(
            f"DCA {pair} FALLBACK DRY RUN",
            f"B_rem: ${b_rem:.4f}\nAll-in est: ${est_cost + est_fee:.4f}\nVol: {vol}"
        ))
        return

    # LIVE fallback market order
    try:
        fb_params = {
            "pair": pair,
            "type": "buy",
            "ordertype": "market",
            "volume": format_volume(vol, pair_info["lot_decimals"]),
            "oflags": "fciq",
            "cl_ordid": fb_cl,
        }
        result = kraken_private("AddOrder", fb_params)
        order_id = result.get("txid", [None])[0]
        update_fb({"status": "placed", "order_id": order_id, "raw": json.dumps(result)})
        print(f"    {ICONS['OK']} fallback placed: {order_id}")
    except KrakenError as e:
        if "duplicate" in str(e).lower():
            found = try_find_kraken_order(fb_cl)
            if found:
                finalize_order(fb_cl, found, mid=ticker.get("mid"), label="FALLBACK FILLED")
                mark("fallback_created")
                return
        update_fb({
            "status": "failed_kraken",
            "reason": f"fallback AddOrder failed: {e}",
            "execution_finished_at": _now_utc_iso(),
            "raw": _failure_raw(fb_params, e, leg="fallback"),
        })
        mark("fallback_failed_kraken")
        tg_send(msg_exec_fail(trade_date, pair, "Nepavyko pateikti atsarginio pavedimo.", e))
        return

    mark("fallback_created")
    time.sleep(2)
    try:
        finalize_order(fb_cl, order_id, mid=ticker.get("mid"), label="FALLBACK FILLED")
    except Exception as e:
        print(f"    fallback finalize error (stale rec will finish): {e}")


def _inspect_dry_limit(row: dict, raw: dict, settings: dict, user_id: str, now_chicago, window_end, deadline):
    """Simulated Kraken responses for the dry-run scenario harness.
    Traverses the SAME statuses as live — only the exchange is faked."""
    scenario = raw.get("maker_scenario", "fill100")
    cl = row["cl_ord_id"]
    vol = float(raw.get("base_volume") or 0)
    px = float(row.get("limit_price") or raw.get("bid") or 0)
    maker_rate = max(float(settings.get("maker_fee_rate") or 0.004), 0.0)

    if scenario == "fill100":
        cost = vol * px
        fee = cost * maker_rate
        sb_update("dca_executions", {"cl_ord_id": f"eq.{cl}"}, {
            "status": "filled_dry_run",
            "filled_quote_cost": round(cost, 6),
            "fee_quote": round(fee, 6),
            "filled_base_volume": vol,
            "avg_price": px,
            "execution_finished_at": _now_utc_iso(),
        })
        tg_send(msg_dryrun(
            f"DCA {row['pair']} MAKER DRY RUN",
            f"Limit filled 100% (sim)\nAll-in est: ${cost + fee:.4f}\nVol: {vol}"
        ))
        return

    # ── Re-peg scenarios (dry): drive the SAME _repeg_decision as live ──
    if scenario in ("repeg_fill", "repeg_reject", "repeg_cap"):
        repeg_count = int(raw.get("repeg_count") or 0)
        # After a successful chase, the re-pegged bid fills as maker.
        if scenario == "repeg_fill" and repeg_count >= 1:
            cost = vol * px
            fee = cost * maker_rate
            sb_update("dca_executions", {"cl_ord_id": f"eq.{cl}"}, {
                "status": "filled_dry_run",
                "filled_quote_cost": round(cost, 6),
                "fee_quote": round(fee, 6),
                "filled_base_volume": vol,
                "avg_price": px,
                "execution_finished_at": _now_utc_iso(),
            })
            tg_send(msg_dryrun(
                f"DCA {row['pair']} MAKER DRY RUN",
                f"Re-peg then filled as maker (sim)\nAll-in est: ${cost + fee:.4f}\nVol: {vol}"
            ))
            print(f"    DRYRUN {cl}: repeg_fill -> maker fill @ {px} (sim)")
            return
        # Before deadline: attempt one chase against a synthetic per-scenario book.
        if now_chicago < deadline:
            repeg_max = int(settings.get("repeg_max") or 5)
            min_ticks = int(settings.get("repeg_min_ticks") or 1)
            cost_target = float(raw.get("cost_target") or (vol * px))
            tick = 10 ** -5
            cap_pct, require_h90 = cap_params(settings)
            # Synthetic books. The repeg_cap reference is set low enough that the
            # veto fires under BOTH cap modes (legacy 3% and the H7+H90 rule),
            # so the scenario keeps its meaning whichever mode is configured.
            if scenario == "repeg_fill":       # bid climbs, still maker, under cap
                s_bid, s_ask, s_ref, s_h90 = px + 3 * tick, px + 4 * tick, px * 1.10, px * 1.10
            elif scenario == "repeg_reject":   # bid meets ask -> spread collapsed
                s_bid, s_ask, s_ref, s_h90 = px + 3 * tick, px + 3 * tick, px * 1.10, px * 1.10
            else:                              # repeg_cap: bid above cap AND above H90
                s_bid, s_ask, s_ref, s_h90 = px + 3 * tick, px + 4 * tick, px * 0.80, px * 0.90
            action, detail = _repeg_decision(
                px, s_bid, s_ask, s_ref, s_h90, cap_pct, require_h90,
                tick, min_ticks, repeg_count, repeg_max, 0.0, cost_target, 8)
            if action == "repeg":
                raw["repeg_count"] = repeg_count + 1
                raw["kraken_cl"] = f"{cl}-r{repeg_count + 1}"
                sb_update("dca_executions", {"cl_ord_id": f"eq.{cl}"}, {
                    "limit_price": detail, "raw": json.dumps(raw),
                })
                print(f"    DRYRUN {cl}: repeg #{repeg_count + 1} {px} -> {detail} (sim)")
            else:
                print(f"    DRYRUN {cl}: repeg skip ({detail}) — waiting (sim)")
            return
        # At/after deadline with no (further) chase: repeg_reject / repeg_cap fall
        # through to the generic nofill cancel + fallback below.

    # partial40 / nofill / crash_after_cancel rest on the book until deadline
    if now_chicago < deadline:
        print(f"    DRYRUN {cl}: open, waiting (deadline {deadline.strftime('%H:%M')})")
        return

    frac = 0.4 if scenario == "partial40" else 0.0
    vol_exec = round(vol * frac, 8)
    cost = vol_exec * px
    fee = cost * maker_rate
    new_status = ("canceled_partial_dry_run" if vol_exec > 0 else "canceled_unfilled_dry_run")
    updates = {
        "status": new_status,
        "execution_finished_at": _now_utc_iso(),
    }
    if vol_exec > 0:
        updates.update({
            "filled_quote_cost": round(cost, 6),
            "fee_quote": round(fee, 6),
            "filled_base_volume": vol_exec,
            "avg_price": px,
        })
    sb_update("dca_executions", {"cl_ord_id": f"eq.{cl}"}, updates)
    print(f"    DRYRUN {cl}: cancel sim -> {new_status} (vol_exec={vol_exec})")

    if scenario == "crash_after_cancel":
        # Simulated crash between cancel and fallback (scenario #5 variant):
        # reason stays NULL — the NEXT run's pending pass must resolve it.
        print("    DRYRUN simulated crash after cancel — decision left pending")
        return

    fresh = dict(row)
    fresh.update(updates)
    _fallback_decision(fresh, settings, user_id, window_end, dry_run=True, scenario=scenario)


# ── Retry after an explicit Kraken rejection ──────────────────────────────────
#
# WHY. On 2026-07-30 AddOrder was refused with EOrder:Insufficient funds at
# 06:53 while the buy window ran to ~07:19. The row went terminal, the claim
# locked the day by cl_ord_id, and the purchase was lost with usable funds still
# on the account for another 26 minutes.
#
# WHY IT REUSES THE ROW. dca_exec_leg_per_event_uniq forbids a second
# maker_limit row per (dca_order_id, trade_date_chicago) -- that is the I3
# double-buy guard and it stays. So a retry rotates the client id on the SAME
# row, exactly as the re-peg path already does.
#
# THE LINE THAT MUST NOT MOVE (DP-3). Retry is allowed ONLY when Kraken said no
# and therefore no order exists. A timeout or a dropped connection is NOT a
# rejection: the order may be live, and retrying it would buy twice. Hence an
# ALLOWLIST of explicit rejections rather than a denylist of failures -- an
# unrecognised error is treated as unknown, which means no retry.
RETRYABLE_KRAKEN_ERRORS = (
    "eorder:insufficient funds",
    "eapi:rate limit exceeded",
    "egeneral:temporary lockout",
    "eservice:unavailable",
    "eservice:busy",
)
RETRY_MAX_DEFAULT = 3


def _failure_raw(params: dict, error, **context) -> str:
    """What was SENT, next to what came back. JSON string for `raw`.

    Failures used to store `{"error": ...}` and nothing else, so the record of
    a rejected order said what Kraken thought of a request nobody kept. On
    2026-07-30 `EOrder:Insufficient funds` was on file with no volume, no
    price and no balance beside it -- every question worth asking afterwards
    (how much did we ask for, against what balance) had to be reconstructed
    from a rotating Actions log. The request is the cheap half of the answer
    and it was the missing one.

    Carries no credentials: the key, nonce and signature are added inside
    `kraken_private` and never appear in these params.
    """
    return json.dumps(_failure_note(params, error, **context))


def _open_orders_digest():
    """What the money is committed to, RIGHT NOW. For failure records only.

    `kraken_open_orders` (v10) keeps the history, but its sync is a manual
    workflow -- so on the morning a buy is refused there is no snapshot from
    that morning. This reads the state at the moment of the refusal and stores
    it beside the error, which is the only place the timing is guaranteed to
    line up.

    Never raises: this runs while something has already gone wrong, and a
    diagnostic that can turn a failure into a crash is worse than no
    diagnostic. A failure here is recorded as its own note.
    """
    try:
        result = kraken_private("OpenOrders")
    except Exception as e:
        return {"error": f"OpenOrders lookup failed: {e}"}
    out = []
    for txid, o in (result.get("open") or {}).items():
        d = o.get("descr") or {}
        vol = float(o.get("vol") or 0)
        vol_exec = float(o.get("vol_exec") or 0)
        # descr.price is the LIMIT price; the top-level `price` is the average
        # fill and reads 0 on an untouched order, which would value every
        # resting order at nothing.
        price = float(d.get("price") or 0)
        out.append({
            "txid": txid,
            "cl_ord_id": o.get("cl_ord_id"),
            "pair": d.get("pair"),
            "side": d.get("type"),
            "ordertype": d.get("ordertype"),
            "price": price,
            "vol": vol,
            "vol_exec": vol_exec,
            "quote_locked": round((vol - vol_exec) * price, 6) if d.get("type") == "buy" else None,
        })
    return out


def snapshot_balances():
    """Balances for a mirror snapshot, preferring the shape that carries holds.

    `BalanceEx` is asked first because it is the only one that reports
    `hold_trade`. `Balance` is the fallback rather than the failure: a snapshot
    without the held amount is worth much more than no snapshot, and the missing
    value is recorded as NULL so the two cannot be confused later.

    Same fallback reasoning as `get_usd_balance`, and deliberately the same
    breadth of `except`: a permission gap on one key must not cost the mirror
    its history.
    """
    try:
        ex = kraken_private("BalanceEx")
        if isinstance(ex, dict) and ex:
            return ex
    except Exception as e:
        print(f"  {ICONS['WARN']} BalanceEx unavailable ({e}) -- snapshot will have no hold_trade")
    return kraken_private("Balance")


def balance_rows(balances, user_id: str, snapshot_ts: str) -> list[dict]:
    """PURE. One `kraken_balances` row per non-zero asset.

    Accepts BOTH shapes Kraken returns. `Balance` gives a scalar per asset;
    `BalanceEx` gives a dict with `balance` and `hold_trade`. Callers prefer
    BalanceEx and fall back, so a builder that understood only one would write an
    empty snapshot for the other and report success doing it.

    `hold_trade` is kept, not discarded. It is the difference between what is
    held and what can be spent, and on 2026-07-30 that difference was the whole
    incident: the USD balance covered the order, the money was in a resting
    limit order, Kraken refused. The mirror recorded the balance and threw the
    held amount away, so the row could not say what had actually happened.

    NULL, never 0.0, when the shape does not carry it. `Balance` omits the field
    entirely, and writing zero there would assert "nothing was held" on exactly
    the snapshots that cannot know.

    Zero balances are dropped: Kraken keeps rows for assets long since sold, and
    a snapshot is about what is held.
    """
    rows = []
    for asset, amount in sorted((balances or {}).items()):
        held = None
        if isinstance(amount, dict):
            raw_held = amount.get("hold_trade")
            amount = amount.get("balance")
            if raw_held is not None:
                try:
                    held = float(raw_held)
                except (TypeError, ValueError):
                    held = None
        try:
            value = float(amount)
        except (TypeError, ValueError):
            continue
        if value != 0:
            rows.append({"user_id": user_id, "snapshot_ts": snapshot_ts,
                         "asset": asset, "balance": value, "hold_trade": held})
    return rows


def open_order_rows(result, user_id: str, snapshot_ts: str) -> list[dict]:
    """PURE. One `kraken_open_orders` row per resting order."""
    rows = []
    for txid, o in ((result or {}).get("open") or {}).items():
        d = o.get("descr") or {}
        rows.append({
            "order_txid": txid,
            "user_id": user_id,
            "snapshot_ts": snapshot_ts,
            "cl_ord_id": o.get("cl_ord_id"),
            "status": o.get("status"),
            "opened_at_utc": datetime.fromtimestamp(
                float(o.get("opentm") or 0), tz=timezone.utc).isoformat(),
            "pair": d.get("pair"),
            "side": d.get("type"),
            "ordertype": d.get("ordertype"),
            # descr.price is the LIMIT price; the top-level `price` is the
            # average fill and reads 0 on an untouched order, which would value
            # every resting order at nothing.
            "price": float(d.get("price") or 0),
            "vol": float(o.get("vol") or 0),
            "vol_exec": float(o.get("vol_exec") or 0),
            "cost": float(o.get("cost") or 0),
            "fee": float(o.get("fee") or 0),
            "oflags": o.get("oflags"),
            "descr": d.get("order"),
            "raw": json.dumps(o),
        })
    return rows


def snapshot_mirror(user_id: str) -> None:
    """Write ONE balance + open-orders snapshot from inside the trading run.

    WHY THIS EXISTS HERE and not only in `kraken_sync.py`. The comment on
    `kraken_open_orders` (db/v10) says the table answers "what was my money
    committed to at 06:53" -- the question the 2026-07-30 refusal could not be
    answered from. The sync then runs once a day at 15:00, so the table could
    never observe the moment it was built for, and after a week it held exactly
    one snapshot. Cadence is not a detail for a point-in-time table; it IS the
    information.

    This run already happens at the moment in question, so the snapshot costs
    two API calls and no new scheduled job.

    ONCE PER DAY, not once per invocation. Cron fires this process every five
    minutes through the window, but the preflight that calls this sits behind
    the day's claim, so later invocations return before reaching it. Putting it
    in `main()` would have written ~120 snapshots a day for the same
    information.

    NEVER RAISES. A snapshot is telemetry; the buy does not depend on it and
    must not be able to fail because of it.
    """
    now = _now_utc_iso()
    try:
        rows = balance_rows(snapshot_balances(), user_id, now)
        for row in rows:
            sb_insert("kraken_balances", row)
        print(f"  {ICONS['OK']} mirror: {len(rows)} balance row(s)")
    except Exception as e:
        print(f"  {ICONS['WARN']} mirror balance snapshot failed: {e}")

    try:
        rows = open_order_rows(kraken_private("OpenOrders"), user_id, now)
        for row in rows:
            sb_insert("kraken_open_orders", row)
        print(f"  {ICONS['OK']} mirror: {len(rows)} resting order(s)")
    except Exception as e:
        # The read-only key needed a separate permission for this; the trading
        # key has it. Still guarded -- see the docstring.
        print(f"  {ICONS['WARN']} mirror open-orders snapshot failed: {e}")


def _failure_note(params: dict, error, **context) -> dict:
    """The same record as a dict, for paths whose `raw` must be MERGED.

    The re-peg leg keeps its counter and history in `raw`; replacing it there
    would reset the count and the leg would re-peg forever. Same trap as the
    retry counter, one function along.
    """
    return {
        "error": str(error),
        "request": {k: v for k, v in (params or {}).items() if k != "nonce"},
        "at": _now_utc_iso(),
        **context,
    }


def _retry_parts(cl_ord_id: str):
    """PURE. Split a client id into (base, attempts already made).

    The counter lives in the id and nowhere else. The obvious place for it was
    `raw`, but the failure handler REPLACES `raw` with `{"error": ...}` on every
    rejection -- so a counter kept there would reset to zero on the very event
    that increments it, and retry_max would never be reached. The id is written
    once per attempt and never overwritten, so it cannot lie.

        dca-KASUSD-2026-07-30-0700        -> (..., 0)
        dca-KASUSD-2026-07-30-0700-r2     -> (..., 2)
    """
    base, sep, tail = (cl_ord_id or "").rpartition("-r")
    if sep and tail.isdigit():
        return (base, int(tail))
    return (cl_ord_id, 0)


def _retry_decision(status, stored_error, retry_count, now_chicago, deadline,
                    retry_max=RETRY_MAX_DEFAULT):
    """PURE. Returns (True, reason) when the failed leg may be re-attempted.

    `deadline` is the same window bound the fallback respects, so a retry can
    never place an order the window would not have allowed.
    """
    if status != "failed_kraken":
        return (False, f"status {status} is not retryable")
    if now_chicago > deadline:
        return (False, "window closed")
    if retry_count >= retry_max:
        return (False, f"retry_max {retry_max} reached")
    low = (stored_error or "").lower()
    if not any(sig in low for sig in RETRYABLE_KRAKEN_ERRORS):
        return (False, "error not an explicit rejection -- order may exist")
    return (True, "explicit rejection, window open")


def _repeg_decision(cur_price, bid, ask, ref_price, h90, cap_pct, require_above_h90,
                    tick, min_ticks, repeg_count, repeg_max, ordermin,
                    cost_target, lot_decimals):
    """PURE decision for MVP bid-chase. No I/O — shared by the live path and
    the dry-run harness so both exercise identical logic.

    Returns (action, detail):
      ('repeg', new_bid)  -> cancel + re-post at new_bid (still post-only)
      ('skip',  reason)   -> leave the resting order as-is this cycle

    Re-peg fires only when the best bid has climbed strictly above our resting
    price (we've been left behind the book), while staying a genuine maker
    (bid < ask) and under the same cap the taker fallback respects."""
    if repeg_count >= repeg_max:
        return ("skip", "repeg_max reached")
    if bid < cur_price + min_ticks * tick:
        return ("skip", "bid not above resting price")   # still top of book
    if bid >= ask:
        return ("skip", "spread collapsed (would cross)")
    capped, cap_detail = cap_decision(bid, ref_price, h90, cap_pct, require_above_h90)
    if capped:
        return ("skip", f"above cap ({cap_detail})")
    new_vol = floor_to_decimals(cost_target / bid, lot_decimals)
    if new_vol <= 0 or new_vol < ordermin:
        return ("skip", "new_vol below ordermin")
    return ("repeg", bid)


def log_repeg_probe(row: dict, action: str, detail: str | None,
                    cur_price=None, bid=None, ask=None, tick=None,
                    repeg_count=None) -> None:
    """Record the re-peg decision for ONE inspection cycle, whatever it was.

    Before this the decision reached a print() and nothing else, so it survived
    only in a GitHub Actions log that rotates — this week's are already gone.
    That is worse than a coarse measurement: it destroys data that would
    otherwise exist, rather than merely limiting future precision.

    Written on EVERY path, including the early returns, because the negatives
    are the measurement. "Rested through five cycles, bid never above the limit"
    is the answer to how often the condition occurs; a fire-only log answers
    nothing. `ticks_above` keeps near-misses visible — 2026-07-21 and -07-28
    both sat at exactly 0.0 ticks, one short of firing, which the executions
    table cannot show.

    Telemetry only, and swallowed on failure: this must never be able to keep a
    leg from being re-pegged or a day from being bought."""
    try:
        ticks_above = None
        if bid is not None and cur_price and tick:
            ticks_above = round((float(bid) - float(cur_price)) / tick, 2)
        sb_insert("dca_repeg_log", {
            "cl_ord_id": row.get("cl_ord_id"),
            "dca_order_id": row.get("dca_order_id"),
            "trade_date_chicago": row.get("trade_date_chicago"),
            "pair": row.get("pair"),
            "limit_price": cur_price,
            "bid": bid,
            "ask": ask,
            "ticks_above": ticks_above,
            "repeg_count": repeg_count,
            "action": action,
            "detail": detail,
        })
    except Exception as e:
        print(f"    {ICONS['WARN']} repeg probe log failed: {e}")


def _maybe_repeg(row: dict, o: dict, settings: dict, user_id: str, window_end) -> bool:
    """LIVE MVP bid-chase. Called only for an open, pre-deadline maker leg.

    Scope (MVP): re-pegs ONLY a fully-unfilled leg. Any partial fill is left
    to the deadline -> fallback path (partial-aware re-peg is a later phase).

    Crash-safety (claim-first): the current order is canceled and confirmed
    zero-fill, then the row is parked as `claimed` (order_id NULL) with
    raw.kraken_cl = the NEXT client id BEFORE AddOrder. So a crash between
    AddOrder and the DB commit is recovered by reconciliation, which searches
    raw.kraken_cl and restores limit_open instead of orphaning the order.

    Returns True if a re-peg was performed (caller stops handling this row
    this cycle); False otherwise (caller falls through to normal 'waiting')."""
    if not bool(settings.get("repeg_enabled")):
        log_repeg_probe(row, "not_evaluated", "repeg_enabled=false")
        return False

    # MVP: zero-fill only (vol_exec from the QueryOrders result we already have)
    if float(o.get("vol_exec", 0) or 0) > 0:
        log_repeg_probe(row, "not_evaluated", "partial fill — MVP re-pegs zero-fill legs only")
        return False

    cl = row["cl_ord_id"]
    pair = row["pair"]
    oid = row.get("order_id")
    if not oid:
        log_repeg_probe(row, "not_evaluated", "no order_id on the row")
        return False
    cur_price = float(row.get("limit_price") or 0)
    if cur_price <= 0:
        log_repeg_probe(row, "not_evaluated", "no limit_price on the row")
        return False

    raw = _safe_json_load(row.get("raw")) or {}
    repeg_count = int(raw.get("repeg_count") or 0)
    repeg_max = int(settings.get("repeg_max") or 5)
    min_ticks = int(settings.get("repeg_min_ticks") or 1)

    try:
        pair_info = get_asset_pair_info(pair)
        ticker = get_ticker_snapshot(pair)
    except Exception as e:
        print(f"    repeg: market data failed ({e}) — skip this cycle")
        log_repeg_probe(row, "not_evaluated", f"market data failed: {e}",
                        cur_price=cur_price, repeg_count=repeg_count)
        return False

    bid, ask = ticker.get("bid"), ticker.get("ask")
    if not bid or not ask or ask <= 0:
        log_repeg_probe(row, "not_evaluated", "no usable bid/ask",
                        cur_price=cur_price, bid=bid, ask=ask, repeg_count=repeg_count)
        return False

    tick = 10 ** (-pair_info["pair_decimals"])
    total_target = float(row["requested_quote_amount_base"])
    safe_total = max(total_target - USD_SAFETY_MARGIN, 0.0)
    maker_rate = max(float(settings.get("maker_fee_rate") or 0.004), 0.0)
    cost_target = safe_total / (1.0 + maker_rate)
    cap_pct, require_h90 = cap_params(settings)
    ref_price, h90, _label = get_cap_context(pair, user_id, settings)

    action, detail = _repeg_decision(
        cur_price, bid, ask, ref_price, h90, cap_pct, require_h90,
        tick, min_ticks, repeg_count, repeg_max, pair_info["ordermin"],
        cost_target, pair_info["lot_decimals"])
    # Logged on BOTH outcomes. The skips are the measurement: "the leg rested
    # through N cycles and the bid was never above it" is what answers how often
    # the condition occurs, and it is exactly what a fire-only log would omit.
    log_repeg_probe(row, action, detail if action != "repeg" else f"repeg to {detail}",
                    cur_price=cur_price, bid=bid, ask=ask, tick=tick,
                    repeg_count=repeg_count)
    if action != "repeg":
        print(f"    repeg skip ({detail}) — {cl} @ {cur_price}")
        return False
    new_bid = detail

    # ── Cancel current resting order, confirm it was zero-fill ──
    try:
        kraken_private("CancelOrder", {"txid": oid})
    except KrakenError as e:
        print(f"    repeg: cancel error (may already be gone): {e}")
    try:
        od = kraken_private("QueryOrders", {"txid": oid})
        oc = od.get(oid) or {}
    except Exception as e:
        print(f"    repeg: readback failed ({e}) — leave for next cycle")
        return False
    st = oc.get("status", "")
    if st == "closed" or float(oc.get("vol_exec", 0) or 0) > 0:
        # Filled (fully or partially) during the cancel race — do NOT re-peg;
        # the normal inspection path finalizes/handles it next cycle.
        print(f"    repeg: fill during cancel race (status={st}) — abort")
        return False
    if st not in ("canceled", "expired"):
        print(f"    repeg: cancel not confirmed (status={st}) — leave for next cycle")
        return False

    new_vol = floor_to_decimals(cost_target / new_bid, pair_info["lot_decimals"])
    new_cl = f"{cl}-r{repeg_count + 1}"
    new_price_str = f"{new_bid:.{pair_info['pair_decimals']}f}"

    # Claim-first: record intent (kraken_cl + parked as claimed) BEFORE AddOrder.
    raw["kraken_cl"] = new_cl
    raw["repeg_count"] = repeg_count + 1
    raw.setdefault("repeg_history", []).append(
        {"n": repeg_count + 1, "from": cur_price, "to": new_bid, "at": _now_utc_iso()})
    sb_update("dca_executions", {"cl_ord_id": f"eq.{cl}"}, {
        "status": "claimed", "order_id": None, "raw": json.dumps(raw),
    })

    repeg_params = {
        "pair": pair, "type": "buy", "ordertype": "limit",
        "price": new_price_str,
        "volume": format_volume(new_vol, pair_info["lot_decimals"]),
        "oflags": "post,fciq",
        "cl_ordid": new_cl,
    }
    try:
        result = kraken_private("AddOrder", repeg_params)
        new_oid = result.get("txid", [None])[0]
    except KrakenError as e:
        # Repost failed/rejected: the old leg is already canceled (zero-fill),
        # so resolve the event straight to a taker fallback for the full budget
        # (mirrors the initial post-only-reject path).
        low = str(e).lower()
        note = "post-only rejected" if "post only" in low else f"AddOrder failed: {e}"
        print(f"    repeg: repost {note} — routing to fallback")
        # MERGE, never replace: raw carries repeg_count and the history, and
        # overwriting it would reset the count so the leg re-pegs forever.
        raw["last_failure"] = _failure_note(repeg_params, e, leg="repeg")
        sb_update("dca_executions", {"cl_ord_id": f"eq.{cl}"}, {
            "status": "rejected_postonly", "order_id": None,
            "limit_price": new_bid,
            "raw": json.dumps(raw),
        })
        fresh = dict(row)
        fresh["status"] = "rejected_postonly"
        _fallback_decision(fresh, settings, user_id, window_end,
                           dry_run=False, scenario=None)
        return True

    # Preserve re-peg tracking (repeg_count/kraken_cl/history) in raw; nest the
    # Kraken result rather than overwriting, or the count would reset next cycle.
    raw["last_result"] = result
    sb_update("dca_executions", {"cl_ord_id": f"eq.{cl}"}, {
        "status": "limit_open", "order_id": new_oid,
        "limit_price": new_bid,
        "bid": bid, "ask": ask, "mid": ticker.get("mid"), "mid_ts": _now_utc_iso(),
        "raw": json.dumps(raw),
    })
    print(f"  {ICONS['OK']} repeg #{repeg_count + 1}: {cur_price} -> {new_bid} "
          f"(txid {new_oid}, vol {new_vol})")
    return True


def run_maker_inspection(settings: dict, user_id: str, now_chicago):
    """Row-driven (NOT flag-driven): runs regardless of order_strategy so a
    rollback flip to 'market' can never strand open maker legs."""
    sel = ("cl_ord_id,order_id,pair,status,trade_date_chicago,"
           "requested_quote_amount_base,parent_event_id,dca_order_id,raw,"
           "execution_started_at,limit_price,mid,filled_quote_cost,fee_quote")

    # Pass 1: pending fallback decisions (crash recovery for the gap
    # between the canceled_*/rejected write and the decision record).
    try:
        pending = sb_get("dca_executions", {
            "user_id": f"eq.{user_id}",
            "status": f"in.({','.join(PENDING_DECISION_STATUSES)})",
            "reason": "is.null",
            "select": sel,
        }) or []
    except Exception as e:
        print(f"  {ICONS['WARN']} pending-decision query failed: {e}")
        pending = []
    for row in pending:
        print(f"  Pending decision: {row['cl_ord_id']} ({row['status']})")
        raw = _safe_json_load(row.get("raw")) or {}
        window_end = _limit_window_end(row, settings)
        _fallback_decision(row, settings, user_id, window_end,
                           dry_run=bool(raw.get("dry_run")),
                           scenario=raw.get("maker_scenario"))

    # Pass 2: open maker legs
    try:
        open_rows = sb_get("dca_executions", {
            "user_id": f"eq.{user_id}",
            "status": "eq.limit_open",
            "select": sel,
        }) or []
    except Exception as e:
        print(f"  {ICONS['WARN']} limit_open query failed: {e}")
        return
    if not open_rows and not pending:
        return
    print(f"\n{ICONS['RECON']} Maker inspection: {len(open_rows)} open, {len(pending)} pending")

    # A snapshot BEFORE any leg is finalized, once per pair with an open leg.
    #
    # WHY HERE. main() writes its snapshots near the end of the run, after this
    # inspection. So resolve_reference_mid could only ever see the PREVIOUS
    # cycle's snapshot, a full cron interval away and therefore outside the
    # +/-180s window -- which made v1.5.0's reference join fall back to the run
    # ticker on every single live fill, exactly the stale reading it was written
    # to replace. Confirmed on 2026-07-29: a KASUSD snapshot existed 4.5s after
    # the fill, written later in the same run, and the join still fell back.
    #
    # Placed inside the inspection rather than by reordering main() because this
    # is the buy path: this adds a read and a write where a cycle already runs,
    # and changes nothing about the order in which orders are placed.
    #
    # Costs one public ticker call per cycle per pair with a resting leg -- a
    # handful a day. Failure is swallowed: telemetry must never keep a leg from
    # being finalized.
    for _snap_pair in {r.get("pair") for r in open_rows if r.get("pair")}:
        try:
            save_mid_snapshot(_snap_pair, get_ticker_snapshot(_snap_pair))
        except Exception as e:
            print(f"  {ICONS['WARN']} pre-inspection snapshot {_snap_pair} failed: {e}")

    for row in open_rows:
        cl = row["cl_ord_id"]
        window_end = _limit_window_end(row, settings)
        deadline = window_end - timedelta(minutes=CRON_CYCLE_MINUTES)
        ttl_at = window_end + timedelta(minutes=LIMIT_TTL_MINUTES)
        raw = _safe_json_load(row.get("raw")) or {}

        if raw.get("dry_run"):
            _inspect_dry_limit(row, raw, settings, user_id, now_chicago, window_end, deadline)
            continue

        oid = row.get("order_id")
        if not oid:
            if now_chicago > ttl_at:
                _mark_manual_required(row, "limit_open be order_id, TTL virsytas")
            continue

        try:
            od = kraken_private("QueryOrders", {"txid": oid})
            o = od.get(oid)
        except Exception as e:
            print(f"  {ICONS['WARN']} {cl}: QueryOrders failed: {e}")
            o = None

        if o is None:
            if now_chicago > ttl_at:
                _mark_manual_required(row, f"orderis {oid} neuzklausiamas, TTL virsytas")
            continue

        st = o.get("status", "")
        if st == "closed":
            print(f"  {cl}: limit filled 100% as maker")
            finalize_order(cl, oid, mid=row.get("mid"))
            continue

        if st in ("canceled", "expired"):
            # Crash recovery: canceled earlier but the row was never updated.
            # Reuse the readback path (idempotent), then decide via Pass 1
            # logic in THIS run to keep normal latency low.
            outcome = _cancel_confirm_readback(row)
            if outcome == "canceled":
                refreshed = sb_get("dca_executions", {"cl_ord_id": f"eq.{cl}", "select": sel})
                if refreshed:
                    _fallback_decision(refreshed[0], settings, user_id, window_end,
                                       dry_run=False, scenario=None)
            continue

        # still open / pending on the book
        if now_chicago > ttl_at:
            outcome = _cancel_confirm_readback(row)
            if outcome == "canceled":
                refreshed = sb_get("dca_executions", {"cl_ord_id": f"eq.{cl}", "select": sel})
                if refreshed:
                    _fallback_decision(refreshed[0], settings, user_id, window_end,
                                       dry_run=False, scenario=None)
            elif outcome is None:
                _mark_manual_required(row, f"limit_open {LIMIT_TTL_MINUTES} min po lango, cancel nepatvirtintas")
            continue

        if now_chicago >= deadline:
            print(f"  {cl}: DEADLINE — cancel sequence")
            outcome = _cancel_confirm_readback(row)
            if outcome == "canceled":
                refreshed = sb_get("dca_executions", {"cl_ord_id": f"eq.{cl}", "select": sel})
                if refreshed:
                    _fallback_decision(refreshed[0], settings, user_id, window_end,
                                       dry_run=False, scenario=None)
            # 'filled' -> done; None -> retry next run, TTL bounds it
        else:
            # Before-deadline & still open: try a bid-chase re-peg (no-op unless
            # repeg_enabled); otherwise keep resting.
            if _maybe_repeg(row, o, settings, user_id, window_end):
                continue
            print(f"  {cl}: open, waiting (deadline {deadline.strftime('%H:%M %Z')})")


# ═══════════════════════════════════════════════════════════════
#  CORE: EXECUTE ONE PAIR
# ═══════════════════════════════════════════════════════════════

def execute_pair(order: dict, settings: dict, today_chicago: str, user_id: str,
                 force: bool = False, daily_burn: float = 0.0) -> dict:
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
    dry_run = bool(settings["dry_run"])

    # Strategy switch (DP-6): default 'market' = pre-Phase-2 behavior.
    # --force always goes market (manual trigger wants immediacy).
    strategy = str(settings.get("order_strategy") or "market")
    maker = strategy == "maker_first" and not force
    fee_rate = (max(float(settings.get("maker_fee_rate") or 0.004), 0.0)
                if maker else float(settings["taker_fee_rate"]))

    ot = (order.get("target_time") or "08:00").replace(":", "")
    if dry_run:
        # Maker dry-run needs a DETERMINISTIC id: one event per order per
        # day, so the cross-run machine is exercised. Market dry-run keeps
        # the historical fill-every-run behavior.
        cl_ord_id = (f"dca-{pair}-{today_chicago}-{ot}-dry" if maker
                     else f"dca-{pair}-{today_chicago}-dry-{int(time.time() * 1000)}")
    else:
        cl_ord_id = f"dca-{pair}-{today_chicago}-force-{int(time.time() * 1000)}" if force else f"dca-{pair}-{today_chicago}-{ot}"

    print(f"\n{'='*50}")
    print(f"  {pair} | ${total_target:.2f} | {strategy} | fee {fee_rate*100:.2f}% | {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  cl_ord_id: {cl_ord_id}")

    # ── Phase 0: take over a leg that Kraken explicitly refused ───────────
    #
    # Same row, rotated client id (dca_exec_leg_per_event_uniq forbids a second
    # maker_limit row for the day, and that guard stays). Only reached when the
    # rejection is on the allowlist and the window is still open; see
    # _retry_decision.
    retry_takeover = False
    if maker and not dry_run and not force:
        try:
            prev = sb_get("dca_executions", {
                "dca_order_id": f"eq.{order.get('id')}",
                "trade_date_chicago": f"eq.{today_chicago}",
                "attempt_type": "eq.maker_limit",
                "select": "id,status,cl_ord_id,reason,parent_event_id",
            })
        except Exception as e:
            prev = []
            print(f"  {ICONS['WARN']} retry lookup failed: {e}")
        if prev:
            prow = prev[0]
            base_cl, rcount = _retry_parts(prow.get("cl_ord_id") or cl_ord_id)
            _, wend = _window_bounds_for(
                today_chicago,
                order.get("target_time") or settings.get("target_time", "08:00"),
                int(order.get("time_window_minutes") or settings.get("time_window_minutes") or 10))
            rdeadline = wend - timedelta(minutes=CRON_CYCLE_MINUTES)
            ok, why = _retry_decision(prow.get("status"), prow.get("reason"),
                                      rcount, datetime.now(CHICAGO_TZ), rdeadline)
            print(f"  Retry check: {'YES' if ok else 'no'} ({why})")
            if ok:
                cl_ord_id = f"{base_cl}-r{rcount + 1}"
                sb_update("dca_executions", {"id": f"eq.{prow['id']}"}, {
                    "cl_ord_id": cl_ord_id,
                    "status": "claimed",
                    "reason": None,
                    "execution_started_at": datetime.now(timezone.utc).isoformat(),
                })
                event_id = prow.get("parent_event_id") or str(uuid.uuid4())
                retry_takeover = True
                print(f"  {ICONS['OK']} Retrying refused leg as {cl_ord_id}")

    # ── Phase 1: CLAIM ────────────────────────────────────────
    if not retry_takeover:
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
        "attempt_type": "maker_limit" if maker else "market",
    }
    if maker:
        # Strategy-unit link: feeds the per-event unique index (I2).
        claim_row["dca_order_id"] = order.get("id")

    try:
        if not retry_takeover:
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
    # Bound BEFORE the try: the except below deliberately continues, and these
    # are read again further down when recording a failure.
    usd_balance, usd_held, bal_source = -1.0, 0.0, "unavailable"

    # One mirror snapshot per day, taken HERE because this is the moment the
    # buy decision is made. It runs before the funding branch below so the
    # record exists on BOTH outcomes -- the 07-30 case that motivated the table
    # was a refusal, not a buy, and a snapshot only on success would have missed
    # exactly the day it was needed. Skipped in dry runs, which decide nothing.
    if not dry_run:
        snapshot_mirror(user_id)

    try:
        usd_balance, usd_held, bal_source = check_balance_usd()
        held_note = f" (held in open orders ${usd_held:.2f})" if usd_held else ""
        print(f"  Spendable: ${usd_balance:.2f}{held_note} | Need: ${total_target:.2f} [{bal_source}]")

        if usd_balance < total_target:
            if dry_run:
                # A dry run spends nothing — real balance must never gate
                # the simulation (2026-07-18 night: all 6 scenario windows
                # died here on a real $0.49 balance).
                print(f"  {ICONS['DRYRUN']} Insufficient REAL balance — continuing, dry run spends nothing")
            else:
                reason = (f"spendable USD ${usd_balance:.2f} < needed ${total_target:.2f}"
                          f"{held_note} [{bal_source}]")
                print(f"  {ICONS['FAIL']} {reason}")
                update_execution({
                    "status": "skipped_insufficient_funds",
                    "reason": reason,
                    "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                    "raw": json.dumps({
                        "spendable_usd": usd_balance,
                        "held_usd": usd_held,
                        "balance_source": bal_source,
                        "needed_usd": total_target,
                        "at": _now_utc_iso(),
                        "open_orders": _open_orders_digest(),
                    }),
                })
                ts_chi = datetime.now(timezone.utc).astimezone(CHICAGO_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
                tg_send(msg_warn(
                    "DCA SKIP",
                    f"{ts_chi} | {pair}\nInsufficient funds: ${usd_balance:.2f} < ${total_target:.2f}"
                    + (f"\nHeld in open orders: ${usd_held:.2f}" if usd_held else "")
                ))
                return {"pair": pair, "status": "skipped_insufficient_funds"}
        # NO LOW-BALANCE WARNING HERE ANY MORE, and its absence is the fix.
        #
        # This is four minutes before the buy, so the figure it reported was the
        # balance about to be spent. On 2026-08-03 it said "$11.66 = ~1.2 days"
        # and the real answer after the fill was $1.66 = zero more buys. Roberto
        # caught it by comparing the message with his own account.
        #
        # The number now appears twice, in the two places where it is true and
        # useful: on the FILL notification (what is actually left, see
        # `finalize_order`) and on the evening run of `kraken_sync`, roughly nine
        # hours before the next buy, which is the last moment a top-up can still
        # be made. Measured reason for the evening rather than midday: 89% of
        # July's shift payouts by value landed after 16:00, so at 13:00 the money
        # that would fund the top-up does not exist yet.
    except KrakenError as e:
        print(f"  {ICONS['WARN']} Balance check failed: {e} — continuing anyway")

    # ── Preflight: Pair info ──────────────────────────────────
    try:
        pair_info = get_asset_pair_info(pair)
        print(f"  Min order: {pair_info['ordermin']} | Lot decimals: {pair_info['lot_decimals']}")
    except KrakenError as e:
        reason = f"AssetPairs lookup failed: {e}"
        fail_action = "Nepavyko gauti poros duomenų iš Kraken."
        print(f"  {ICONS['FAIL']} {reason}")
        update_execution({
            "status": "failed_kraken",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
        })
        tg_send(msg_exec_fail(today_chicago, pair, fail_action, e))
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

    # Store the context in the SAME run that derives cap_price from it. Written
    # here and not at fill time because the fill is normally observed by a later
    # cron cycle: a second fetch returns a slightly different H7, and the row's
    # arithmetic (cap_price / h7 - 1 == cap_pct) stops holding. Unconditional on
    # the cap branch below, so `force` runs and reference-less days still carry
    # market context. finalize_order will not overwrite what lands here.
    if ohlc_ctx:
        update_execution({
            "h7": ohlc_ctx.get("H7"),
            "h30": ohlc_ctx.get("H30"),
            "ohlc_ts": datetime.now(timezone.utc).isoformat(),
        })

    # ── Cap Check (veto layer, Smart DCA) ──────────────────────
    if ticker["mid"] and not force:
        cap_pct, require_h90 = cap_params(settings)
        ref_price, h90, label = get_cap_context(pair, user_id, settings, ohlc_ctx)
        if ref_price is not None:
            h90_txt = f"${h90:.6f}" if h90 else "n/a"
            print(f"  {label} ref: ${ref_price:.6f} | Cap: ${ref_price * (1 + cap_pct):.6f} | "
                  f"H90: {h90_txt} | Mid: ${ticker['mid']:.6f}")
            skip, detail = cap_decision(ticker["mid"], ref_price, h90, cap_pct, require_h90)
            # Recorded on BOTH outcomes: a buy row used to say nothing about the
            # cap, so "did the H90 guard save this day?" was unanswerable later.
            cap_cols = cap_telemetry(ref_price, cap_pct, h90)
            if skip:
                pct_over = ((ticker["mid"] / ref_price) - 1) * 100
                reason = f"Mid ${ticker['mid']:.6f} above cap: {detail} [{label}]"
                print(f"  {ICONS['SKIP']} {reason}")
                update_execution({
                    "status": "skipped_above_cap",
                    "reason": reason,
                    "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                    **cap_cols,
                })
                tg_send(f"{ICONS['SKIP']} {pair.replace('USD','')} +{pct_over:.2f}% virš cap – skip")
                return {"pair": pair, "status": "skipped_above_cap"}
            print(f"  {ICONS['OK']} Below cap — proceeding")
            update_execution(cap_cols)
        else:
            print(f"  No {label} reference — skipping cap check")
    # ── Fee-aware volume calculation ──────────────────────────
    # Maker leg prices/sizes at BID (the limit rests there); market at ASK.
    price_ref = ticker["bid"] if maker else ticker["ask"]
    if not price_ref or price_ref <= 0:
        reason = f"No valid {'BID' if maker else 'ASK'} price – cannot compute base volume"
        fail_action = "Nepavyko apskaičiuoti kiekio: nėra galiojančios kainos."
        print(f"  {ICONS['FAIL']} {reason}")
        update_execution({
            "status": "failed_kraken",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
        })
        tg_send(msg_exec_fail(today_chicago, pair, fail_action, reason))
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

    base_volume = floor_to_decimals(cost_target / price_ref, pair_info["lot_decimals"])

    if base_volume <= 0:
        reason = (
            f"Computed base_volume is 0 after rounding "
            f"(cost_target={cost_target:.6f}, price_ref={price_ref}, lot_decimals={pair_info['lot_decimals']})"
        )
        print(f"  {ICONS['FAIL']} {reason}")
        update_execution({
            "status": "skipped_target_too_small",
            "reason": reason,
            "execution_finished_at": datetime.now(timezone.utc).isoformat(),
        })
        tg_send(msg_warn("DCA SKIP", f"{today_chicago} | {pair}\n{reason}"))
        return {"pair": pair, "status": "skipped_target_too_small"}

    estimated_cost = base_volume * price_ref
    estimated_fee = estimated_cost * fee_rate
    estimated_total = estimated_cost + estimated_fee

    print(f"  Fee-aware calc (with safety margin):")
    print(f"    total_target:   ${total_target:.4f}")
    print(f"    safety_margin:  ${USD_SAFETY_MARGIN:.4f}")
    print(f"    safe_total:     ${safe_total:.4f}")
    print(f"    cost_target:    ${cost_target:.4f}")
    print(f"    price_ref:      ${price_ref} ({'bid/maker' if maker else 'ask/taker'})")
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

    # ── MAKER LIMIT LEG (Phase 2, T0) ─────────────────────────
    if maker:
        o_window = int(order.get("time_window_minutes") or settings.get("time_window_minutes", 10))
        o_time = order.get("target_time") or settings.get("target_time", "08:00")
        _, window_end = _window_bounds_for(today_chicago, o_time, o_window)
        limit_price = ticker["bid"]
        price_str = f"{limit_price:.{pair_info['pair_decimals']}f}"

        def _pending_row():
            return {
                "pair": pair,
                "cl_ord_id": cl_ord_id,
                "trade_date_chicago": today_chicago,
                "requested_quote_amount_base": total_target,
                "filled_quote_cost": None,
                "fee_quote": None,
                "parent_event_id": event_id,
                "dca_order_id": order.get("id"),
            }

        if dry_run:
            scenario = _pick_dry_scenario(user_id)
            print(f"  {ICONS['DRYRUN']} scenario: {scenario}")
            if scenario == "reject":
                print(f"  {ICONS['DRYRUN']} DRYRUN post-only reject (sim) -> direct fallback")
                update_execution({
                    "status": "rejected_postonly_dry_run",
                    "limit_price": limit_price,
                    "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                    "raw": json.dumps({"dry_run": True, "maker_scenario": scenario}),
                })
                _fallback_decision(_pending_row(), settings, user_id, window_end,
                                   dry_run=True, scenario=scenario)
                return {"pair": pair, "status": "rejected_postonly_dry_run"}

            print(f"  {ICONS['DRYRUN']} DRYRUN limit resting (sim, scenario={scenario}): {base_volume} @ bid {price_str}")
            update_execution({
                "status": "limit_open",
                "limit_price": limit_price,
                "raw": json.dumps({
                    "dry_run": True,
                    "maker_scenario": scenario,
                    "base_volume": base_volume,
                    "bid": limit_price,
                    "cost_target": round(cost_target, 6),
                }),
            })
            return {"pair": pair, "status": "limit_open"}

        limit_params = {
            "pair": pair,
            "type": "buy",
            "ordertype": "limit",
            "price": price_str,
            "volume": format_volume(base_volume, pair_info["lot_decimals"]),
            "oflags": "post,fciq",
            "cl_ordid": cl_ord_id,
        }
        try:
            result = kraken_private("AddOrder", limit_params)
            order_id = result.get("txid", [None])[0]
            print(f"  {ICONS['OK']} Post-only limit resting: {order_id} @ {price_str}")
            update_execution({
                "status": "limit_open",
                "order_id": order_id,
                "limit_price": limit_price,
                # The preflight reading is recorded on the SUCCESS path too.
                # Failures carry it since v1.7.0, but a successful run left no
                # trace of which balance source was used -- so "the spendable
                # balance check is live" could only be shown from an Actions log
                # that rotates. Additive: the re-peg machinery reads its own
                # keys out of this same `raw` and is unaffected.
                "raw": json.dumps({**result, "preflight": {
                    "spendable_usd": usd_balance,
                    "held_usd": usd_held,
                    "balance_source": bal_source,
                }}),
            })
            # No finalize here — the order rests; the next runs' inspection
            # phase owns it from now on (DP-2: cron cadence is the timer).
            return {"pair": pair, "status": "limit_open", "order_id": order_id}
        except KrakenError as e:
            if "post only" in str(e).lower():
                # Would cross the spread -> rejected, NO position exists.
                print(f"  {ICONS['SKIP']} Post-only rejected -> direct fallback")
                update_execution({
                    "status": "rejected_postonly",
                    "limit_price": limit_price,
                    "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                    "raw": _failure_raw(limit_params, e, spendable_usd=usd_balance,
                                        held_usd=usd_held, balance_source=bal_source),
                })
                _fallback_decision(_pending_row(), settings, user_id, window_end,
                                   dry_run=False, scenario=None)
                return {"pair": pair, "status": "rejected_postonly"}
            reason = f"limit AddOrder failed: {e}"
            print(f"  {ICONS['FAIL']} {reason}")
            update_execution({
                "status": "failed_kraken",
                "reason": reason,
                "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                "raw": _failure_raw(limit_params, e, spendable_usd=usd_balance,
                                    held_usd=usd_held, balance_source=bal_source,
                                    open_orders=_open_orders_digest()),
            })
            tg_send(msg_exec_fail(today_chicago, pair, "Nepavyko pateikti pavedimo.", e))
            return {"pair": pair, "status": "failed_kraken"}

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
            "raw": _failure_raw(order_params, e, spendable_usd=usd_balance,
                                held_usd=usd_held, balance_source=bal_source,
                                open_orders=_open_orders_digest()),
        })
        tg_send(msg_exec_fail(today_chicago, pair, "Nepavyko pateikti pavedimo.", e))
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
        "select": "cl_ord_id,order_id,pair,status,trade_date_chicago,attempt_type,raw",
    })

    if not stale:
        print("  No stale executions found.")
        return

    for row in stale:
        cl_id = row["cl_ord_id"]
        order_id = row.get("order_id")
        print(f"  Stale: {cl_id} | status={row['status']} | order_id={order_id}")

        if row["status"] == "claimed" and not order_id:
            raw = _safe_json_load(row.get("raw")) or {}
            if raw.get("dry_run"):
                # Dry-run scenario #6 (crash after fb submit): simulated
                # reconciliation must finalize WITHOUT re-buying.
                sb_update("dca_executions", {"cl_ord_id": f"eq.{cl_id}"}, {
                    "status": "filled_dry_run",
                    "filled_quote_cost": raw.get("estimated_cost"),
                    "fee_quote": raw.get("estimated_fee"),
                    "filled_base_volume": raw.get("base_volume"),
                    "avg_price": raw.get("ask"),
                    "execution_finished_at": datetime.now(timezone.utc).isoformat(),
                })
                print("    DRYRUN stale claim -> simulated reconciliation finalize")
                tg_send(msg_recon(
                    "DCA RECONCILIATION (DRY)",
                    f"{row['trade_date_chicago']} | {row['pair']}\nSimulated crash recovered without re-buy"
                ))
                continue
            # A re-peg parks the row as `claimed` with raw.kraken_cl = the
            # client id of the re-posted order (differs from the row key). Search
            # by that id so a crash mid-repeg recovers the resting order instead
            # of orphaning it (double-buy guard).
            search_cl = raw.get("kraken_cl") or cl_id
            found = try_find_kraken_order(search_cl)
            if found:
                print("    Found in Kraken! Finalizing...")
                finalize_order(cl_id, found)
            elif row.get("attempt_type") == "maker_limit":
                # A resting limit is INVISIBLE to ClosedOrders. If AddOrder
                # succeeded but the DB update crashed, the order is open on
                # Kraken — restore the row to limit_open so the state
                # machine owns it again instead of orphaning the order.
                open_tx = _find_open_kraken_order(search_cl)
                if open_tx:
                    print(f"    Found RESTING on Kraken ({open_tx}) — restoring limit_open")
                    sb_update("dca_executions", {"cl_ord_id": f"eq.{cl_id}"}, {
                        "status": "limit_open",
                        "order_id": open_tx,
                    })
                elif (recheck := try_find_kraken_order(search_cl)):
                    # Race: filled between the ClosedOrders miss and the
                    # OpenOrders miss. Without this re-check the fill would
                    # be marked failed with real money spent.
                    print(f"    Closed during race window ({recheck}) — finalizing")
                    finalize_order(cl_id, recheck)
                else:
                    print("    Not found in Kraken (closed or open) — marking failed")
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

        # NOTE: exact-match on fill statuses. The old `"filled" in status`
        # substring test would wrongly count canceled_unfilled as a fill.
        # canceled_partial carries real money, so it aggregates as a fill;
        # unfilled/rejected maker legs are non-events (their fallback leg
        # carries the day's outcome).
        if status in ("filled", "filled_dry_run", "canceled_partial", "canceled_partial_dry_run"):
            s["filled"] += 1
            s["total_cost"] += float(r.get("filled_quote_cost") or 0)
            s["total_fee"] += float(r.get("fee_quote") or 0)
            s["total_vol"] += float(r.get("filled_base_volume") or 0)
            mid = float(r.get("mid") or 0)
            avg = float(r.get("avg_price") or 0)
            if mid > 0 and avg > 0:
                s["slippages"].append((avg - mid) / mid * 100)
        elif "skipped" in status or status in (
            "canceled_unfilled", "canceled_unfilled_dry_run",
            "rejected_postonly", "rejected_postonly_dry_run",
        ):
            s["skipped"] += 1
        elif "failed" in status or "crashed" in status or status == "manual_required":
            s["failed"] += 1

    lines = [
        f"{ICONS['CHART']} {B_ON}DCA Weekly Summary{B_OFF}",
        f"Week: {week_key}",
        "",
    ]

    for pair, s in pairs.items():
        symbol = pair.replace("USD", "")
        all_in = s["total_cost"] + s["total_fee"]
        avg_eff = (all_in / s["total_vol"]) if s["total_vol"] > 0 else 0
        avg_slip = (sum(s["slippages"]) / len(s["slippages"])) if s["slippages"] else 0

        lines.append(f"{B_ON}{symbol}{B_OFF}")
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
        run_maker_inspection(settings, user_id, datetime.now(CHICAGO_TZ))
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

    # 1b) Maker-leg inspection (row-driven; no-op when no maker rows exist)
    run_maker_inspection(settings, user_id, now_chicago)

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

    # Total committed per day across every enabled order -- the burn rate the
    # low-balance warning measures the Kraken USD balance against.
    daily_burn = sum(float(o.get("base_quote_amount") or 0) for o in orders)

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
            result = execute_pair(order, settings, today_chicago, user_id,
                                  force=(mode == "--force"), daily_burn=daily_burn)
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
