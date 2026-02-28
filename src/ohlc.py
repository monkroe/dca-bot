#!/usr/bin/env python3
"""
ohlc.py - Kraken public OHLC utilities (stdlib-only)

Purpose:
- Fetch daily OHLC from Kraken public API (interval=1440)
- Fill missing daily closes via forward-fill (ffill) to keep Kraken-market-consistent series
- Compute SMA (H7/H30/H90/H180) and simple percentiles

Notes:
- This module does NOT place orders; it only provides reference series and metrics.
- Forward-fill is intentional (no CoinGecko fallback) to preserve venue-consistent analytics.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
DAILY_INTERVAL_MIN = 1440


@dataclass(frozen=True)
class OhlcPoint:
    d: date
    close: float


class OhlcError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_unix_ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _http_get_json(url: str, timeout_s: int = 15) -> Dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "robert-dca-bot/ohlc-stdlib",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _kraken_pair_key(result: Dict) -> str:
    # Kraken returns {"result": {"XBTUSD": [...], "last": ...}} or similar.
    # We need the non-"last" key.
    keys = [k for k in result.keys() if k != "last"]
    if not keys:
        raise OhlcError("Kraken OHLC result missing pair key")
    # Usually exactly one key.
    return keys[0]


def fetch_ohlc(
    pair: str,
    days: int,
    *,
    timeout_s: int = 15,
    max_retries: int = 2,
) -> List[OhlcPoint]:
    """
    Fetch daily close prices for the last `days` days (plus small buffer) from Kraken OHLC.

    Returns: List[OhlcPoint] sorted ascending by date.
    """
    if days <= 0:
        raise ValueError("days must be > 0")

    # Ask Kraken for candles since (today - (days + buffer)) at 00:00 UTC.
    # Buffer helps Kraken "last" cursor and ensures we can fill exactly requested range.
    buffer_days = 3
    since_dt = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days + buffer_days)
    since = _to_unix_ts(since_dt)

    url = f"{KRAKEN_OHLC_URL}?pair={pair}&interval={DAILY_INTERVAL_MIN}&since={since}"

    attempt = 0
    backoff_s = 1.0
    last_err: Optional[Exception] = None

    while attempt <= max_retries:
        try:
            payload = _http_get_json(url, timeout_s=timeout_s)
            if not isinstance(payload, dict):
                raise OhlcError("Kraken OHLC returned non-dict JSON")

            errs = payload.get("error")
            if errs:
                # Kraken returns error list like ["EQuery:Unknown asset pair"]
                raise OhlcError("Kraken OHLC error: " + "; ".join(map(str, errs)))

            result = payload.get("result")
            if not isinstance(result, dict):
                raise OhlcError("Kraken OHLC missing result")

            pair_key = _kraken_pair_key(result)
            rows = result.get(pair_key)
            if not isinstance(rows, list):
                raise OhlcError("Kraken OHLC result rows missing")

            points: List[OhlcPoint] = []
            for r in rows:
                # Each r: [time, open, high, low, close, vwap, volume, count]
                # time is unix seconds.
                if not isinstance(r, list) or len(r) < 5:
                    continue
                ts = int(r[0])
                close = float(r[4])
                d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                points.append(OhlcPoint(d=d, close=close))

            # Deduplicate by date (keep last in case of duplicates), then sort.
            by_day: Dict[date, float] = {}
            for p in points:
                by_day[p.d] = p.close
            out = [OhlcPoint(d=k, close=v) for k, v in sorted(by_day.items(), key=lambda kv: kv[0])]

            if not out:
                raise OhlcError("Kraken OHLC returned empty series")

            return out

        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OhlcError, ValueError) as e:
            last_err = e
            if attempt == max_retries:
                break
            time.sleep(backoff_s)
            backoff_s *= 2.0
            attempt += 1

    raise OhlcError(f"Failed to fetch OHLC for {pair} after {max_retries+1} attempts: {last_err}")


def ffill_gaps(points: Sequence[OhlcPoint], start: date, end: date) -> List[OhlcPoint]:
    """
    Forward-fill daily closes between [start, end] (inclusive).
    If Kraken has no candle for a day, we carry previous day's close.
    """
    if start > end:
        raise ValueError("start must be <= end")
    if not points:
        raise ValueError("points must be non-empty")

    by_day: Dict[date, float] = {p.d: p.close for p in points}
    cur = start
    out: List[OhlcPoint] = []

    # Seed: find nearest available close at or before start
    seed_day = cur
    seed_close: Optional[float] = None
    # Walk backwards up to 14 days to find a seed (buffer).
    for _ in range(14):
        if seed_day in by_day:
            seed_close = by_day[seed_day]
            break
        seed_day = seed_day - timedelta(days=1)
    if seed_close is None:
        # Fall back to earliest available close in input series
        earliest = min(by_day.keys())
        seed_close = by_day[earliest]

    prev_close = seed_close

    while cur <= end:
        if cur in by_day:
            prev_close = by_day[cur]
            out.append(OhlcPoint(d=cur, close=prev_close))
        else:
            out.append(OhlcPoint(d=cur, close=prev_close))
        cur = cur + timedelta(days=1)

    return out


def calc_sma(points: Sequence[OhlcPoint], period: int) -> Optional[float]:
    """
    Simple moving average of the last `period` closes.
    Returns None if not enough points.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(points) < period:
        return None
    tail = points[-period:]
    s = sum(p.close for p in tail)
    return s / float(period)


def calc_percentile(values: Sequence[float], p: float) -> float:
    """
    Percentile with linear interpolation.
    p in [0, 100]. Returns a value at that percentile.
    """
    if not values:
        raise ValueError("values must be non-empty")
    if p < 0 or p > 100:
        raise ValueError("p must be between 0 and 100")

    xs = sorted(float(x) for x in values)
    if len(xs) == 1:
        return xs[0]

    rank = (p / 100.0) * (len(xs) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(xs) - 1)
    frac = rank - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def build_daily_metrics(
    pair: str,
    *,
    days: int = 220,
    sma_periods: Sequence[int] = (7, 30, 90, 180),
) -> Dict[str, float]:
    """
    Convenience helper: fetch + ffill + compute SMA metrics.
    Returns dict with keys like H7/H30/H90/H180 and also latest_close.
    """
    raw = fetch_ohlc(pair, days)
    end = _utc_now().date()
    start = end - timedelta(days=days - 1)
    series = ffill_gaps(raw, start=start, end=end)

    closes = [p.close for p in series]
    out: Dict[str, float] = {"latest_close": closes[-1]}

    for per in sma_periods:
        v = calc_sma(series, per)
        if v is not None:
            out[f"H{per}"] = v

    # Percentiles (optional but useful)
    out["p25"] = calc_percentile(closes, 25)
    out["p50"] = calc_percentile(closes, 50)
    out["p75"] = calc_percentile(closes, 75)

    return out
