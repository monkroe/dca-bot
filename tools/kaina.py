#!/usr/bin/env python3
"""kaina — show what the DCA bot sees right now, without waiting for its window.

Live bid/ask from Kraken, the H7/H30/H90 daily-close standard from ohlc.py,
and the cap verdict computed by the bot's OWN cap_decision(), so this tool can
never drift from what actually runs.

Usage:
    kaina                    # the live order's pair
    kaina KAS BTC SOL        # bare symbols or full Kraken pairs
    kaina KAS --cap-pct 0.10 # what-if on the threshold
    kaina KAS --no-h90       # what-if without the guard

Installed as the `kaina` command by tools/install-kaina.sh.
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))


def _load_dotenv(path):
    """Minimal KEY=VALUE reader -- no dependency, and the shell environment
    still wins so an explicit export can override the file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass


_load_dotenv(os.path.expanduser("~/.env"))

# kraken_run reads its secrets at import time, but everything this tool touches
# is pure. setdefault, so a real environment still wins.
for _k in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET", "SUPABASE_URL",
           "SUPABASE_SERVICE_ROLE_KEY"):
    os.environ.setdefault(_k, "unused-by-this-tool")

from ohlc import build_daily_metrics                      # noqa: E402
from kraken_run import cap_decision, cap_params           # noqa: E402

try:
    CHICAGO = ZoneInfo("America/Chicago")
except Exception:  # Termux without tzdata -- a header should never be fatal
    CHICAGO = timezone.utc
DEFAULT_PAIR = "KASUSD"
TICKER_URL = "https://api.kraken.com/0/public/Ticker?pair="

# What production is configured to, NOT kraken_run's defaults. Those are
# deliberately legacy-safe so a deploy changes nothing until the flip UPDATE
# runs -- correct for the bot, misleading for an operator tool, which should
# answer "would it buy?" the way the live settings would. Only used when this
# tool cannot reach dca_settings; the DB always wins when credentials exist.
# Truth lives in dca_settings -- the label on the Cap line says which was used,
# so a stale assumption here shows up rather than passing as fact.
ASSUMED_SETTINGS = {
    "cap_mode": "ohlc_h7",
    "cap_pct": 0.20,
    "cap_require_above_h90": True,
}


# "kaina KAS" should just work. Kraken wants a full pair, and BTC is XBT there.
SYMBOL_ALIASES = {"BTC": "XBT", "DOGE": "XDG"}


def to_pair(arg):
    s = arg.strip().upper()
    for quote in ("USDT", "USD", "EUR"):
        if s.endswith(quote) and len(s) > len(quote):
            base = s[:-len(quote)]
            return SYMBOL_ALIASES.get(base, base) + quote
    return SYMBOL_ALIASES.get(s, s) + "USD"


def ticker(pair):
    with urllib.request.urlopen(TICKER_URL + pair, timeout=15) as r:
        data = json.load(r)
    if data.get("error"):
        raise RuntimeError("; ".join(data["error"]))
    result = data.get("result") or {}
    if not result:
        raise RuntimeError("pair not found")
    # Kraken answers XBTUSD as XXBTZUSD, so take the single value, not the key.
    t = next(iter(result.values()))
    bid, ask = float(t["b"][0]), float(t["a"][0])
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2,
            "open24": float(t["o"]), "last": float(t["c"][0])}


def live_settings():
    """Cap config from dca_settings, or None if no credentials here."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url.startswith("http") or key == "unused-by-this-tool":
        return None
    req = urllib.request.Request(
        f"{url}/rest/v1/dca_settings?select=cap_mode,cap_pct,cap_require_above_h90&limit=1",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.load(r)
        return rows[0] if rows else None
    except Exception:
        return None


def money(v):
    return f"${v:,.5f}" if v < 1 else f"${v:,.2f}"


def pct(a, b):
    """a relative to b, signed."""
    return (a / b - 1.0) * 100.0


def report(pair, settings, source, cap_pct_override, no_h90):
    t = ticker(pair)
    m = build_daily_metrics(pair, days=220)
    h7, h30, h90 = m.get("H7"), m.get("H30"), m.get("H90")

    cap_pct, require_h90 = cap_params(settings or {})
    if cap_pct_override is not None:
        cap_pct = cap_pct_override
    if no_h90:
        require_h90 = False
    mode = (settings.get("cap_mode") or "exec_7d") if settings else "exec_7d"

    print(f"\n{pair}   {money(t['mid'])}   24h {pct(t['last'], t['open24']):+.2f}%")
    print(f"  bid {money(t['bid'])}   ask {money(t['ask'])}   "
          f"spread {pct(t['ask'], t['bid']):.3f}%")

    print()
    for name, v in (("H7", h7), ("H30", h30), ("H90", h90)):
        if v is None:
            print(f"  {name:<4} n/a")
            continue
        print(f"  {name:<4} {money(v):<12} mid {pct(t['mid'], v):+6.2f}%")

    if h7 and h30 and h90:
        if h7 < h30 < h90:
            print("  trendas  H7 < H30 < H90 — leidziasi (kaupti pigu)")
        elif h7 > h30 > h90:
            print("  trendas  H7 > H30 > H90 — kyla")
        else:
            print("  trendas  mix — be aiskios krypties")

    # ── the verdict, from the bot's own function ──────────────
    print()
    if mode != "ohlc_h7":
        print(f"  cap rezimas '{mode}' — sis irankis rodo tik H7 taisykle")
    if h7 is None:
        print("  Cap  n/a — nera H7, botas pirktu (DP-4)")
        return
    cap_price = h7 * (1.0 + cap_pct)
    guard = "H90 ijungta" if require_h90 else "H90 isjungta"
    print(f"  Cap  {money(cap_price)}   (H7 x {1 + cap_pct:.2f}, {guard})   [{source}]")

    skip, detail = cap_decision(t["mid"], h7, h90, cap_pct, require_h90)
    if skip:
        print(f"  NEPIRKTU — {detail}")
    else:
        headroom = pct(cap_price, t["mid"])
        line = f"  PIRKTU — mid {headroom:.1f}% zemiau cap"
        # Say WHY when the guard is the only thing holding the skip back.
        if t["mid"] > cap_price and require_h90 and h90 and t["mid"] <= h90:
            line = (f"  PIRKTU — virs cap, BET po H90 ({pct(t['mid'], h90):+.1f}%), "
                    f"apsauga sulaiko skipa")
        print(line)


def main():
    ap = argparse.ArgumentParser(description="What the DCA bot sees right now.")
    ap.add_argument("pairs", nargs="*", default=[], help=f"KAS, BTC, SOL or full pairs (default {DEFAULT_PAIR})")
    ap.add_argument("--cap-pct", type=float, default=None, help="what-if cap threshold, e.g. 0.10")
    ap.add_argument("--no-h90", action="store_true", help="what-if: cap without the H90 guard")
    args = ap.parse_args()

    settings = live_settings()
    source = "gyvi nustatymai"
    if settings is None:
        settings = dict(ASSUMED_SETTINGS)
        source = "prielaida, ne is DB"

    print(f"\n{datetime.now(CHICAGO):%Y-%m-%d %H:%M %Z}", end="")
    for pair in (args.pairs or [DEFAULT_PAIR]):
        try:
            report(to_pair(pair), settings, source, args.cap_pct, args.no_h90)
        except Exception as e:
            print(f"\n{to_pair(pair)}   klaida: {e}")
    print()


if __name__ == "__main__":
    main()
