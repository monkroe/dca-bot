#!/usr/bin/env python3
"""Fail-closed Kraken OHLC preservation collector.

This module is deliberately separate from ``ohlc.py`` and the DCA execution
path.  It preserves only committed KASUSD daily and four-hour Kraken candles in
``dca_ohlc_history``.  It never fills gaps, substitutes another provider,
updates an existing row, or performs DDL.

Production runs are permitted only after an external, read-only evidence gate
has established the exact database UNIQUE authority
``(pair, interval_minutes, ts)``.  The collector neither queries PostgreSQL
catalogs through PostgREST nor treats its own pre-write manifest as independent
provider evidence.

The public functions are intentionally side-effect-free where possible so the
contract can be exercised hermetically.  Network I/O exists only behind the
three small clients near the end of the file and is entered only by ``main``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PAIR = "KASUSD"
DAILY_INTERVAL = 1440
FOUR_HOUR_INTERVAL = 240
INTERVALS = (DAILY_INTERVAL, FOUR_HOUR_INTERVAL)
DAILY_TARGET_MIN_TS = "2024-11-19T00:00:00Z"
FOUR_HOUR_TARGET_MIN_TS = "2026-04-04T04:00:00Z"
LEGACY_SOURCE = "kraken_ohlc_backfill"
CATCHUP_SOURCE = "kraken_ohlc_catchup"
INCREMENTAL_SOURCE = "kraken_ohlc_incremental"
CATCHUP_SOURCE_ALLOWLIST = (LEGACY_SOURCE, CATCHUP_SOURCE)
INCREMENTAL_SOURCE_ALLOWLIST = (
    LEGACY_SOURCE,
    CATCHUP_SOURCE,
    INCREMENTAL_SOURCE,
)
LEGACY_CREATED_AT = "2026-07-17T13:31:41.756482Z"
BASELINE_COMMIT_SHA = "00bce458c7beb50aa27d071b213693d45e43d1ae"
REPOSITORY = "monkroe/dca-bot"
WORKFLOW_PATH = ".github/workflows/kraken_ohlc_preserve.yml"
TABLE = "dca_ohlc_history"
SCHEMA_VERSION = "ohlc-preservation-manifest-v1.3"
ANCHOR_SCHEMA_VERSION = "ohlc-preservation-anchor-v1.3"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
PAGE_SIZE = 1000
ARTIFACT_RETENTION_DAYS = 90

MODES = (
    "dry-run",
    "catch-up-apply",
    "catch-up-resume",
    "incremental-apply",
    "incremental-resume",
    "head-rebuild",
)
APPLY_MODES = ("catch-up-apply", "catch-up-resume",
               "incremental-apply", "incremental-resume")
RESUME_MODES = ("catch-up-resume", "incremental-resume")

DECIMAL_FIELDS = ("open", "high", "low", "close", "vwap", "volume")
MARKET_FIELDS = DECIMAL_FIELDS + ("trade_count",)
CANONICAL_FIELDS = (
    "pair",
    "interval_minutes",
    "ts",
    "open",
    "high",
    "low",
    "close",
    "vwap",
    "volume",
    "trade_count",
    "source",
)

STATE_A = {
    DAILY_INTERVAL: {
        "count": 103,
        "min": "2026-04-05T00:00:00Z",
        "max": "2026-07-16T00:00:00Z",
    },
    FOUR_HOUR_INTERVAL: {
        "count": 626,
        "min": "2026-04-04T04:00:00Z",
        "max": "2026-07-17T08:00:00Z",
    },
}


class ContractError(RuntimeError):
    """A named fail-loud contract violation."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Confirmation:
    mode: str
    run_id: int | None = None
    run_attempt: int | None = None


@dataclass(frozen=True)
class RuntimeAuthority:
    repository: str
    ref_name: str
    execution_commit_sha: str
    daily_target_min_ts: str | None = None


@dataclass(frozen=True)
class FetchResult:
    committed: tuple[dict[str, Any], ...]
    rejected_current: tuple[dict[str, Any], ...]
    duplicate_keys: tuple[tuple[str, int, str], ...]

    @property
    def total(self) -> int:
        return len(self.committed) + len(self.rejected_current)


@dataclass(frozen=True)
class Anchor:
    interval_minutes: int
    min_ts: str
    accepted_through_ts: str
    count: int
    cumulative_digest: str


@dataclass(frozen=True)
class AnchorBundle:
    identity: str
    predecessor_identity: str | None
    predecessor_digest: str | None
    genesis_identity: str
    run_id: int
    run_attempt: int
    source_allowlist: tuple[str, ...]
    anchors: Mapping[int, Anchor]
    artifact_id: int | None = None
    artifact_digest: str | None = None


def parse_utc(value: str | datetime, *, candle: bool = False) -> datetime:
    """Parse an aware UTC instant and optionally require whole seconds."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(" UTC"):
            text = text[:-4] + "+00:00"
        elif text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ContractError("MALFORMED_TIMESTAMP", str(value)) from exc
    else:
        raise ContractError("MALFORMED_TIMESTAMP", repr(value))
    if parsed.tzinfo is None:
        raise ContractError("MALFORMED_TIMESTAMP", "timezone is required")
    parsed = parsed.astimezone(timezone.utc)
    if candle and parsed.microsecond:
        raise ContractError("MALFORMED_TIMESTAMP", "candle timestamp has fractions")
    return parsed


def canonical_ts(value: str | datetime) -> str:
    return parse_utc(value, candle=True).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_decimal(value: Any) -> str | None:
    """Canonical arbitrary-precision decimal, never exponent notation."""
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ContractError("MALFORMED_DECIMAL", repr(value))
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ContractError("MALFORMED_DECIMAL", repr(value)) from exc
    if not number.is_finite():
        raise ContractError("MALFORMED_DECIMAL", repr(value))
    if number == 0:
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered.startswith("+"):
        rendered = rendered[1:]
    return rendered


def normalize_integer(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ContractError("MALFORMED_INTEGER", repr(value))
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        raise ContractError("MALFORMED_INTEGER", repr(value))
    return int(text, 10)


def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    missing = [field for field in CANONICAL_FIELDS if field not in row]
    if missing:
        raise ContractError("MALFORMED_ROW", "missing " + ",".join(missing))
    interval = normalize_integer(row["interval_minutes"])
    if interval not in INTERVALS:
        raise ContractError("MALFORMED_ROW", f"unsupported interval {interval}")
    normalized: dict[str, Any] = {
        "pair": str(row["pair"]),
        "interval_minutes": interval,
        "ts": canonical_ts(row["ts"]),
    }
    for field in DECIMAL_FIELDS:
        normalized[field] = normalize_decimal(row[field])
    normalized["trade_count"] = normalize_integer(row["trade_count"])
    source = row["source"]
    if source is not None and not isinstance(source, str):
        raise ContractError("MALFORMED_ROW", "source must be string or null")
    normalized["source"] = source
    return normalized


def canonical_json_line(row: Mapping[str, Any]) -> bytes:
    normalized = normalize_row(row)
    rendered: list[str] = []
    for field in CANONICAL_FIELDS:
        value = normalized[field]
        key = json.dumps(field, ensure_ascii=False)
        if field in DECIMAL_FIELDS:
            encoded = "null" if value is None else value
        elif field in ("interval_minutes", "trade_count"):
            encoded = "null" if value is None else str(value)
        else:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        rendered.append(f"{key}:{encoded}")
    return ("{" + ",".join(rendered) + "}\n").encode("utf-8")


def canonical_rowset(rows: Iterable[Mapping[str, Any]]) -> bytes:
    normalized = [normalize_row(row) for row in rows]
    normalized.sort(key=lambda row: (
        row["pair"], row["interval_minutes"], row["ts"]
    ))
    return b"".join(canonical_json_line(row) for row in normalized)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def row_digest(row: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_line(row))


def rowset_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    return sha256_hex(canonical_rowset(rows))


def row_key(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(row["pair"]),
        int(row["interval_minutes"]),
        canonical_ts(row["ts"]),
    )


def market_fields_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    for field in DECIMAL_FIELDS:
        if normalize_decimal(left.get(field)) != normalize_decimal(right.get(field)):
            return False
    return normalize_integer(left.get("trade_count")) == normalize_integer(
        right.get("trade_count")
    )


def floor_utc(value: str | datetime, interval_minutes: int) -> datetime:
    if interval_minutes not in INTERVALS:
        raise ContractError("INVALID_INTERVAL", str(interval_minutes))
    instant = parse_utc(value)
    seconds = interval_minutes * 60
    floored = int(instant.timestamp()) // seconds * seconds
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def expected_latest_committed_ts(
    run_started_at: str | datetime, interval_minutes: int
) -> str:
    latest = floor_utc(run_started_at, interval_minutes) - timedelta(
        minutes=interval_minutes
    )
    return canonical_ts(latest)


def boundary_settlement(run_started_at: str | datetime) -> str | None:
    """Return the next allowed UTC start during [4h boundary, +5 minutes)."""
    started = parse_utc(run_started_at)
    boundary = floor_utc(started, FOUR_HOUR_INTERVAL)
    allowed = boundary + timedelta(minutes=5)
    if started < allowed:
        return canonical_ts(allowed)
    return None


def on_grid(ts: str | datetime, interval_minutes: int) -> bool:
    instant = parse_utc(ts, candle=True)
    return int(instant.timestamp()) % (interval_minutes * 60) == 0


def parse_confirmation(mode: str, confirmation: str) -> Confirmation:
    if mode not in MODES:
        raise ContractError("INVALID_MODE", mode)
    confirmation = confirmation or ""
    exact = {
        "dry-run": "",
        "catch-up-apply": "APPLY catch-up",
        "incremental-apply": "APPLY incremental",
        "head-rebuild": "REBUILD head",
    }
    if mode in exact:
        if confirmation != exact[mode]:
            raise ContractError("CONFIRMATION_MISMATCH", mode)
        return Confirmation(mode)
    family = "catch-up" if mode == "catch-up-resume" else "incremental"
    match = re.fullmatch(
        rf"RESUME {re.escape(family)} RUN_ID=([1-9]\d*) RUN_ATTEMPT=([1-9]\d*)",
        confirmation,
    )
    if not match:
        raise ContractError("CONFIRMATION_MISMATCH", mode)
    return Confirmation(mode, int(match.group(1)), int(match.group(2)))


def validate_dispatch_inputs(inputs: Mapping[str, Any]) -> None:
    forbidden = {
        "daily_target_min_ts", "genesis", "genesis_anchor",
        "source_allowlist", "run_id", "run_attempt",
    }
    extra = set(inputs) - {"mode", "confirmation"}
    if extra:
        code = "FORBIDDEN_DISPATCH_INPUT" if extra & forbidden else "UNKNOWN_DISPATCH_INPUT"
        raise ContractError(code, ",".join(sorted(extra)))


def validate_daily_target(value: str | None) -> str:
    if not value:
        raise ContractError("MISSING_DAILY_TARGET")
    try:
        text = value.strip()
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("daily target must carry UTC")
        canonical = canonical_ts(value)
    except (ContractError, ValueError) as exc:
        raise ContractError("MALFORMED_DAILY_TARGET", str(value)) from exc
    if not on_grid(canonical, DAILY_INTERVAL):
        raise ContractError("OFF_GRID_DAILY_TARGET", canonical)
    return canonical


def validate_source_allowlists(catchup: str, incremental: str) -> None:
    first = tuple(part for part in catchup.split(",") if part)
    second = tuple(part for part in incremental.split(",") if part)
    if first != CATCHUP_SOURCE_ALLOWLIST or second != INCREMENTAL_SOURCE_ALLOWLIST:
        raise ContractError("SOURCE_ALLOWLIST_MISMATCH")


def _timestamp_from_kraken(value: Any) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise ContractError("MALFORMED_KRAKEN_ROW", "timestamp")
    try:
        seconds = int(str(value), 10)
    except (TypeError, ValueError) as exc:
        raise ContractError("MALFORMED_KRAKEN_ROW", "timestamp") from exc
    if str(value).strip() not in (str(seconds), f"+{seconds}"):
        raise ContractError("MALFORMED_KRAKEN_ROW", "timestamp")
    return canonical_ts(datetime.fromtimestamp(seconds, tz=timezone.utc))


def parse_kraken_payload(
    payload: Mapping[str, Any], interval_minutes: int,
    run_started_at: str | datetime, *, intended_source: str,
) -> FetchResult:
    if interval_minutes not in INTERVALS:
        raise ContractError("INVALID_INTERVAL", str(interval_minutes))
    if not isinstance(payload, Mapping) or payload.get("error"):
        raise ContractError("KRAKEN_RESPONSE_ERROR", repr(payload.get("error")))
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ContractError("KRAKEN_RESPONSE_ERROR", "missing result")
    pair_keys = [key for key in result if key != "last"]
    if len(pair_keys) != 1 or not isinstance(result[pair_keys[0]], list):
        raise ContractError("KRAKEN_RESPONSE_ERROR", "ambiguous pair rows")
    started = parse_utc(run_started_at)
    committed: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    counts: dict[tuple[str, int, str], int] = {}
    for raw in result[pair_keys[0]]:
        if not isinstance(raw, list) or len(raw) < 8:
            raise ContractError("MALFORMED_KRAKEN_ROW", repr(raw))
        ts = _timestamp_from_kraken(raw[0])
        if not on_grid(ts, interval_minutes):
            raise ContractError("OFF_GRID_KRAKEN_ROW", ts)
        row: dict[str, Any] = {
            "pair": PAIR,
            "interval_minutes": interval_minutes,
            "ts": ts,
            "open": raw[1],
            "high": raw[2],
            "low": raw[3],
            "close": raw[4],
            "vwap": raw[5],
            "volume": raw[6],
            "trade_count": raw[7],
            "source": intended_source,
        }
        row = normalize_row(row)
        if any(row[field] is None for field in MARKET_FIELDS):
            raise ContractError("MALFORMED_KRAKEN_ROW", "null market field")
        key = row_key(row)
        counts[key] = counts.get(key, 0) + 1
        closes_at = parse_utc(ts) + timedelta(minutes=interval_minutes)
        (committed if closes_at <= started else current).append(row)
    duplicates = tuple(sorted(key for key, count in counts.items() if count > 1))
    committed.sort(key=row_key)
    current.sort(key=row_key)
    return FetchResult(tuple(committed), tuple(current), duplicates)


def duplicate_keys(rows: Iterable[Mapping[str, Any]]) -> tuple[tuple[str, int, str], ...]:
    counts: dict[tuple[str, int, str], int] = {}
    for row in rows:
        key = row_key(row)
        counts[key] = counts.get(key, 0) + 1
    return tuple(sorted(key for key, count in counts.items() if count > 1))


def timestamps_between(start: str, end: str, interval_minutes: int) -> list[str]:
    first = parse_utc(start, candle=True)
    last = parse_utc(end, candle=True)
    if first > last:
        return []
    out: list[str] = []
    current = first
    step = timedelta(minutes=interval_minutes)
    while current <= last:
        out.append(canonical_ts(current))
        current += step
    return out


def continuity_gaps(
    rows: Iterable[Mapping[str, Any]], start: str, end: str, interval_minutes: int
) -> list[str]:
    have = {
        canonical_ts(row["ts"])
        for row in rows
        if row["pair"] == PAIR and int(row["interval_minutes"]) == interval_minutes
    }
    return [ts for ts in timestamps_between(start, end, interval_minutes) if ts not in have]


def gap_classification(gaps: Sequence[str], recoverable_minimum: str | None) -> str:
    if not gaps:
        return "CONTIGUOUS"
    if recoverable_minimum is None or parse_utc(gaps[0]) < parse_utc(recoverable_minimum):
        return "PERMANENT_GAP"
    return "RECOVERABLE_GAP"


def gap_summary(gaps: Sequence[str]) -> dict[str, Any]:
    return {
        "count": len(gaps),
        "first": gaps[0] if gaps else None,
        "last": gaps[-1] if gaps else None,
    }


def _legacy_created_matches(value: Any) -> bool:
    try:
        return parse_utc(value) == parse_utc(LEGACY_CREATED_AT)
    except ContractError:
        return False


def validate_state_a(rows_by_interval: Mapping[int, Sequence[Mapping[str, Any]]]) -> None:
    for interval in INTERVALS:
        rows = list(rows_by_interval.get(interval, ()))
        spec = STATE_A[interval]
        if not rows:
            raise ContractError("SILENT_EMPTY_DB_READ", str(interval))
        if duplicate_keys(rows):
            raise ContractError("DB_DUPLICATE_KEYS", str(interval))
        keys = sorted(canonical_ts(row["ts"]) for row in rows)
        if len(rows) != spec["count"] or keys[0] != spec["min"] or keys[-1] != spec["max"]:
            raise ContractError("BASELINE_CONFLICT", str(interval))
        if continuity_gaps(rows, spec["min"], spec["max"], interval):
            raise ContractError("BASELINE_CONFLICT", f"{interval} continuity")
        for row in rows:
            if (row.get("pair") != PAIR
                    or int(row.get("interval_minutes", 0)) != interval
                    or row.get("source") != LEGACY_SOURCE
                    or not _legacy_created_matches(row.get("created_at"))
                    or not on_grid(row["ts"], interval)):
                raise ContractError("BASELINE_CONFLICT", str(interval))


def validate_state_b(
    rows_by_interval: Mapping[int, Sequence[Mapping[str, Any]]],
    manifest: Mapping[str, Any], family: str,
) -> None:
    validate_manifest(manifest)
    expected_source = CATCHUP_SOURCE if family == "catch-up" else INCREMENTAL_SOURCE
    authorized = manifest_rows(manifest)
    for interval in INTERVALS:
        rows = list(rows_by_interval[interval])
        legacy = [row for row in rows if row.get("source") == LEGACY_SOURCE]
        if family == "catch-up":
            spec = STATE_A[interval]
            if (not legacy or len(legacy) != spec["count"]
                    or min(canonical_ts(r["ts"]) for r in legacy) != spec["min"]
                    or max(canonical_ts(r["ts"]) for r in legacy) != spec["max"]
                    or continuity_gaps(legacy, spec["min"], spec["max"], interval)
                    or any(not _legacy_created_matches(r.get("created_at")) for r in legacy)):
                raise ContractError("BASELINE_CONFLICT", str(interval))
        for row in rows:
            if row.get("source") == LEGACY_SOURCE:
                continue
            key = row_key(row)
            expected = authorized.get(key)
            if expected is None or row.get("source") != expected_source:
                raise ContractError("UNVERIFIABLE_HISTORIC", str(key))
            if row_digest(expected) != row_digest(row) or not market_fields_equal(
                row, expected
            ):
                raise ContractError("MANIFEST_MISMATCH", str(key))


def verify_anchor_prefix(
    anchor: Anchor, rows: Sequence[Mapping[str, Any]], source_allowlist: Sequence[str]
) -> None:
    if duplicate_keys(rows):
        raise ContractError("ACCEPTED_ANCHOR_MISMATCH", "duplicate key")
    expected_keys = timestamps_between(
        anchor.min_ts, anchor.accepted_through_ts, anchor.interval_minutes
    )
    actual_keys = sorted(canonical_ts(row["ts"]) for row in rows)
    if (len(rows) != anchor.count or len(rows) != len(expected_keys)
            or actual_keys != expected_keys):
        raise ContractError("ACCEPTED_ANCHOR_MISMATCH", "count/min/through/key set")
    if any(row.get("source") not in source_allowlist for row in rows):
        raise ContractError("ACCEPTED_ANCHOR_MISMATCH", "source")
    if any(row.get("source") == LEGACY_SOURCE
           and not _legacy_created_matches(row.get("created_at")) for row in rows):
        raise ContractError("ACCEPTED_ANCHOR_MISMATCH", "legacy created_at")
    if rowset_digest(rows) != anchor.cumulative_digest:
        raise ContractError("ACCEPTED_ANCHOR_MISMATCH", "digest")


def anchor_from_dict(interval: int, value: Mapping[str, Any]) -> Anchor:
    try:
        anchor = Anchor(
            interval_minutes=interval,
            min_ts=canonical_ts(value["min_ts"]),
            accepted_through_ts=canonical_ts(value["accepted_through_ts"]),
            count=int(value["count"]),
            cumulative_digest=str(value["cumulative_digest"]),
        )
    except (KeyError, TypeError, ValueError, ContractError) as exc:
        raise ContractError("MALFORMED_ANCHOR", str(interval)) from exc
    if (not re.fullmatch(r"[0-9a-f]{64}", anchor.cumulative_digest)
            or anchor.count <= 0 or not on_grid(anchor.min_ts, interval)
            or not on_grid(anchor.accepted_through_ts, interval)):
        raise ContractError("MALFORMED_ANCHOR", str(interval))
    return anchor


def parse_genesis_constants(env: Mapping[str, str]) -> AnchorBundle | None:
    names = (
        "GENESIS_IDENTITY", "GENESIS_ARTIFACT_ID", "GENESIS_ARTIFACT_NAME",
        "GENESIS_ARTIFACT_DIGEST",
        "GENESIS_RUN_ID", "GENESIS_RUN_ATTEMPT",
        "GENESIS_1440_MIN_TS", "GENESIS_1440_THROUGH_TS", "GENESIS_1440_COUNT",
        "GENESIS_1440_CUMULATIVE_SHA256", "GENESIS_240_MIN_TS",
        "GENESIS_240_THROUGH_TS", "GENESIS_240_COUNT",
        "GENESIS_240_CUMULATIVE_SHA256",
    )
    values = [env.get(name, "") for name in names]
    if all(value == "UNINITIALIZED" for value in values):
        return None
    if any(not value or value == "UNINITIALIZED" for value in values):
        raise ContractError("MISSING_GENESIS", "partially initialized constants")
    try:
        artifact_id = int(env["GENESIS_ARTIFACT_ID"])
        run_id = int(env["GENESIS_RUN_ID"])
        attempt = int(env["GENESIS_RUN_ATTEMPT"])
    except ValueError as exc:
        raise ContractError("MALFORMED_ANCHOR", "genesis identity") from exc
    artifact_digest = env["GENESIS_ARTIFACT_DIGEST"]
    if (artifact_id <= 0 or run_id <= 0 or attempt <= 0
            or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", artifact_digest)):
        raise ContractError("MALFORMED_ANCHOR", "artifact digest")
    anchors = {
        DAILY_INTERVAL: anchor_from_dict(DAILY_INTERVAL, {
            "min_ts": env["GENESIS_1440_MIN_TS"],
            "accepted_through_ts": env["GENESIS_1440_THROUGH_TS"],
            "count": env["GENESIS_1440_COUNT"],
            "cumulative_digest": env["GENESIS_1440_CUMULATIVE_SHA256"],
        }),
        FOUR_HOUR_INTERVAL: anchor_from_dict(FOUR_HOUR_INTERVAL, {
            "min_ts": env["GENESIS_240_MIN_TS"],
            "accepted_through_ts": env["GENESIS_240_THROUGH_TS"],
            "count": env["GENESIS_240_COUNT"],
            "cumulative_digest": env["GENESIS_240_CUMULATIVE_SHA256"],
        }),
    }
    identity = env["GENESIS_IDENTITY"]
    if identity != f"run:{run_id}:attempt:{attempt}:candidate_genesis":
        raise ContractError("MALFORMED_ANCHOR", "genesis identity")
    if env["GENESIS_ARTIFACT_NAME"] != f"ohlc-candidate-genesis-{run_id}-{attempt}":
        raise ContractError("MALFORMED_ANCHOR", "genesis artifact name")
    if anchors[FOUR_HOUR_INTERVAL].min_ts != FOUR_HOUR_TARGET_MIN_TS:
        raise ContractError("MALFORMED_ANCHOR", "genesis minimum")
    return AnchorBundle(
        identity=identity,
        predecessor_identity=None,
        predecessor_digest=None,
        genesis_identity=identity,
        run_id=run_id,
        run_attempt=attempt,
        source_allowlist=CATCHUP_SOURCE_ALLOWLIST,
        anchors=anchors,
        artifact_id=artifact_id,
        artifact_digest=artifact_digest.removeprefix("sha256:"),
    )


def canonical_document(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def seal_document(value: Mapping[str, Any], digest_field: str) -> dict[str, Any]:
    document = dict(value)
    document.pop(digest_field, None)
    document[digest_field] = sha256_hex(canonical_document(document))
    return document


def validate_sealed_document(value: Mapping[str, Any], digest_field: str) -> None:
    claimed = value.get(digest_field)
    unsigned = dict(value)
    unsigned.pop(digest_field, None)
    actual = sha256_hex(canonical_document(unsigned))
    if not isinstance(claimed, str) or claimed != actual:
        raise ContractError("MANIFEST_DIGEST_MISMATCH", digest_field)


def _manifest_entries(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {"row": normalized, "row_sha256": row_digest(normalized)}
        for normalized in sorted((normalize_row(row) for row in rows), key=row_key)
    ]


def _lineage_identity(
    manifest: Mapping[str, Any], artifact: Mapping[str, Any],
) -> dict[str, Any]:
    digest = str(artifact.get("digest", "")).removeprefix("sha256:")
    identity = {
        "run_id": manifest.get("run_id"),
        "run_attempt": manifest.get("run_attempt"),
        "mode": manifest.get("mode"),
        "artifact_id": artifact.get("id"),
        "artifact_name": artifact.get("name"),
        "artifact_digest": digest,
        "manifest_sha256": manifest.get("manifest_sha256"),
        "execution_commit_sha": manifest.get("execution_commit_sha"),
    }
    _validate_lineage_identity(identity)
    return identity


def _validate_lineage_identity(identity: Mapping[str, Any]) -> None:
    try:
        run_id = int(identity["run_id"])
        attempt = int(identity["run_attempt"])
        artifact_id = int(identity["artifact_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("MANIFEST_MISMATCH", "lineage identity") from exc
    mode = identity.get("mode")
    expected_name = f"ohlc-prewrite-{run_id}-{attempt}-{mode}"
    if (run_id <= 0 or attempt <= 0 or artifact_id <= 0
            or mode not in APPLY_MODES
            or identity.get("artifact_name") != expected_name
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("artifact_digest", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("manifest_sha256", "")))
            or not re.fullmatch(
                r"[0-9a-f]{40}", str(identity.get("execution_commit_sha", ""))
            )):
        raise ContractError("MANIFEST_MISMATCH", "lineage identity")


def build_manifest(
    *, run_id: int, run_attempt: int, mode: str, run_started_at: str,
    execution_commit_sha: str, daily_target_min_ts: str,
    predecessor_identity: str, predecessor_digest: str,
    expected_latest: Mapping[int, str], recoverable_minimum: Mapping[int, str],
    intended_source: str, rows: Sequence[Mapping[str, Any]],
    predecessor_evidence: Sequence[
        tuple[Mapping[str, Any], Mapping[str, Any]]
    ] = (),
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", execution_commit_sha):
        raise ContractError("INVALID_EXECUTION_COMMIT_SHA", execution_commit_sha)
    target = validate_daily_target(daily_target_min_ts)
    entries = _manifest_entries(rows)
    authorized_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    lineage_by_run: dict[tuple[int, int], dict[str, Any]] = {}
    for predecessor_manifest, artifact in predecessor_evidence:
        validate_manifest(predecessor_manifest)
        for identity in predecessor_manifest["authorized_lineage"]:
            _validate_lineage_identity(identity)
            lineage_by_run[(int(identity["run_id"]), int(identity["run_attempt"]))] = dict(
                identity
            )
        identity = _lineage_identity(predecessor_manifest, artifact)
        lineage_by_run[(int(identity["run_id"]), int(identity["run_attempt"]))] = identity
        for entry in predecessor_manifest["authorized_rows"]:
            key = row_key(entry["row"])
            previous = authorized_by_key.get(key)
            if previous is not None and previous != entry:
                raise ContractError("MANIFEST_MISMATCH", "lineage row conflict")
            authorized_by_key[key] = dict(entry)
    for entry in entries:
        key = row_key(entry["row"])
        previous = authorized_by_key.get(key)
        if previous is not None and previous != entry:
            raise ContractError("MANIFEST_MISMATCH", "intended row conflict")
        authorized_by_key[key] = entry
    authorized_entries = [
        authorized_by_key[key] for key in sorted(authorized_by_key)
    ]
    lineage = [
        lineage_by_run[key] for key in sorted(lineage_by_run)
    ]
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo": REPOSITORY,
        "baseline_commit_sha": BASELINE_COMMIT_SHA,
        "execution_commit_sha": execution_commit_sha,
        "workflow_path": WORKFLOW_PATH,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "mode": mode,
        "run_started_at": canonical_ts(run_started_at),
        "daily_target_min_ts": target,
        "predecessor_identity": predecessor_identity,
        "predecessor_cumulative_digest": predecessor_digest,
        "expected_latest_committed_ts": {
            str(key): canonical_ts(value) for key, value in expected_latest.items()
        },
        "recoverable_minimum": {
            str(key): canonical_ts(value) for key, value in recoverable_minimum.items()
        },
        "intended_source": intended_source,
        "would_insert_keys": [list(row_key(entry["row"])) for entry in entries],
        "intended_rows": entries,
        "authorized_lineage": lineage,
        "authorized_rows": authorized_entries,
    }
    return seal_document(body, "manifest_sha256")


def validate_manifest(
    manifest: Mapping[str, Any], *, run_id: int | None = None,
    run_attempt: int | None = None,
) -> None:
    validate_sealed_document(manifest, "manifest_sha256")
    if (manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("repo") != REPOSITORY
            or manifest.get("baseline_commit_sha") != BASELINE_COMMIT_SHA
            or manifest.get("workflow_path") != WORKFLOW_PATH):
        raise ContractError("MANIFEST_MISMATCH", "authority fields")
    if not re.fullmatch(
        r"[0-9a-f]{40}", str(manifest.get("execution_commit_sha", ""))
    ):
        raise ContractError("MANIFEST_MISMATCH", "execution commit")
    try:
        validate_daily_target(manifest.get("daily_target_min_ts"))
    except ContractError as exc:
        raise ContractError("MANIFEST_MISMATCH", "daily target") from exc
    mode = manifest.get("mode")
    if mode not in APPLY_MODES:
        raise ContractError("MANIFEST_MISMATCH", "mode")
    expected_source = (CATCHUP_SOURCE if str(mode).startswith("catch-up")
                       else INCREMENTAL_SOURCE)
    if manifest.get("intended_source") != expected_source:
        raise ContractError("MANIFEST_MISMATCH", "intended source")
    if set(manifest.get("expected_latest_committed_ts", {})) != {"1440", "240"}:
        raise ContractError("MANIFEST_MISMATCH", "expected latest intervals")
    if set(manifest.get("recoverable_minimum", {})) != {"1440", "240"}:
        raise ContractError("MANIFEST_MISMATCH", "recoverable intervals")
    if run_id is not None and manifest.get("run_id") != run_id:
        raise ContractError("RUN_ID_MISMATCH")
    if run_attempt is not None and manifest.get("run_attempt") != run_attempt:
        raise ContractError("RUN_ATTEMPT_MISMATCH")
    entries = manifest.get("intended_rows")
    authorized = manifest.get("authorized_rows")
    lineage = manifest.get("authorized_lineage")
    if not isinstance(entries, list) or not isinstance(authorized, list):
        raise ContractError("MANIFEST_MISMATCH", "manifest rows")
    if not isinstance(lineage, list):
        raise ContractError("MANIFEST_MISMATCH", "lineage")
    lineage_keys = []
    for identity in lineage:
        if not isinstance(identity, Mapping):
            raise ContractError("MANIFEST_MISMATCH", "lineage identity")
        _validate_lineage_identity(identity)
        if not str(identity["mode"]).startswith(str(mode).split("-", 1)[0]):
            raise ContractError("MANIFEST_MISMATCH", "lineage family")
        lineage_keys.append((int(identity["run_id"]), int(identity["run_attempt"])))
    if len(lineage_keys) != len(set(lineage_keys)):
        raise ContractError("MANIFEST_MISMATCH", "duplicate lineage")

    def validate_entries(values: Sequence[Mapping[str, Any]]) -> list[list[Any]]:
        keys: list[list[Any]] = []
        for entry in values:
            if (not isinstance(entry, Mapping) or "row" not in entry
                    or entry.get("row_sha256") != row_digest(entry["row"])):
                raise ContractError("MANIFEST_MISMATCH", "row digest")
            if entry["row"].get("source") != expected_source:
                raise ContractError("MANIFEST_MISMATCH", "row source")
            keys.append(list(row_key(entry["row"])))
        if len(keys) != len({tuple(key) for key in keys}) or keys != sorted(keys):
            raise ContractError("MANIFEST_MISMATCH", "row order or duplicate")
        return keys

    keys = validate_entries(entries)
    authorized_keys = validate_entries(authorized)
    authorized_map = {
        tuple(key): entry for key, entry in zip(authorized_keys, authorized)
    }
    for entry in entries:
        key = row_key(entry["row"])
        if authorized_map.get(key) != entry:
            raise ContractError("MANIFEST_MISMATCH", "intended authorization")
    if not lineage and authorized != entries:
        raise ContractError("MANIFEST_MISMATCH", "unbound authorized rows")
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("row_sha256") != row_digest(entry["row"]):
            raise ContractError("MANIFEST_MISMATCH", "row digest")
        if entry["row"].get("source") != expected_source:
            raise ContractError("MANIFEST_MISMATCH", "row source")
    if keys != manifest.get("would_insert_keys") or len(keys) != len({tuple(k) for k in keys}):
        raise ContractError("MANIFEST_MISMATCH", "keys")


def manifest_rows(manifest: Mapping[str, Any]) -> dict[tuple[str, int, str], Mapping[str, Any]]:
    validate_manifest(manifest)
    return {row_key(entry["row"]): entry["row"] for entry in manifest["authorized_rows"]}


def classify_existing_rows(
    rows: Sequence[Mapping[str, Any]], *, accepted_keys: set[tuple[str, int, str]],
    fetched_rows: Mapping[tuple[str, int, str], Mapping[str, Any]],
    historic_manifests: Sequence[Mapping[str, Any]], source_allowlist: Sequence[str],
) -> dict[str, Any]:
    if duplicate_keys(rows):
        raise ContractError("DB_DUPLICATE_KEYS")
    historic: dict[tuple[str, int, str], Mapping[str, Any]] = {}
    for manifest in historic_manifests:
        historic.update(manifest_rows(manifest))
    result: dict[str, Any] = {
        "accepted_anchor": 0,
        "recoverable_verifiable": 0,
        "manifest_verifiable_historic": 0,
        "unverifiable_historic": [],
        "matching_existing": 0,
        "mismatched_existing": [],
    }
    for row in rows:
        key = row_key(row)
        if row.get("source") not in source_allowlist:
            raise ContractError("SOURCE_NOT_ALLOWED", str(key))
        if key in accepted_keys:
            result["accepted_anchor"] += 1
            continue
        if key in fetched_rows:
            if market_fields_equal(row, fetched_rows[key]):
                result["recoverable_verifiable"] += 1
                result["matching_existing"] += 1
            else:
                result["mismatched_existing"].append(key)
            continue
        historic_row = historic.get(key)
        if (historic_row is not None and market_fields_equal(row, historic_row)
                and row.get("source") == historic_row.get("source")
                and row_digest(row) == row_digest(historic_row)):
            result["manifest_verifiable_historic"] += 1
        else:
            result["unverifiable_historic"].append(key)
    return result


def verify_race(existing: Mapping[str, Any] | None, intended: Mapping[str, Any]) -> str:
    if existing is None:
        return "missing"
    if (existing.get("source") == intended.get("source")
            and market_fields_equal(existing, intended)):
        return "race_identical"
    raise ContractError("RACE_MISMATCH", str(row_key(intended)))


def advance_verified_through(
    predecessor_through: str, intended_timestamps: Sequence[str],
    verified_timestamps: set[str], interval_minutes: int,
) -> str:
    through = parse_utc(predecessor_through)
    step = timedelta(minutes=interval_minutes)
    intended = {canonical_ts(ts) for ts in intended_timestamps}
    while canonical_ts(through + step) in intended:
        candidate = canonical_ts(through + step)
        if candidate not in verified_timestamps:
            break
        through += step
    return canonical_ts(through)


def artifact_retention_ok(created_at: str, expires_at: str) -> bool:
    created = parse_utc(created_at)
    expires = parse_utc(expires_at)
    return expires - created >= timedelta(days=ARTIFACT_RETENTION_DAYS)


def verify_artifact_metadata(
    metadata: Mapping[str, Any], *, artifact_id: int, name: str,
    platform_digest: str,
) -> None:
    if int(metadata.get("id", -1)) != artifact_id or metadata.get("name") != name:
        raise ContractError("ARTIFACT_IDENTITY_MISMATCH")
    if metadata.get("expired") is True:
        raise ContractError("ARTIFACT_EXPIRED")
    actual_digest = str(metadata.get("digest", "")).removeprefix("sha256:")
    expected_digest = platform_digest.removeprefix("sha256:")
    if (not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
            or actual_digest != expected_digest):
        raise ContractError("ARTIFACT_DIGEST_MISMATCH")
    if not artifact_retention_ok(str(metadata.get("created_at")),
                                 str(metadata.get("expires_at"))):
        raise ContractError("ARTIFACT_RETENTION_TOO_SHORT")


def accept_head_candidate(
    document: Mapping[str, Any], *, workflow_run: Mapping[str, Any],
    genesis_identity: str,
) -> AnchorBundle:
    validate_sealed_document(document, "head_sha256")
    if (document.get("schema_version") != ANCHOR_SCHEMA_VERSION
            or document.get("kind") != "operational_head"):
        raise ContractError("MALFORMED_ANCHOR")
    if (workflow_run.get("conclusion") != "success"
            or workflow_run.get("head_branch") != "main"
            or int(workflow_run.get("id", 0)) != int(document["run_id"])
            or int(workflow_run.get("run_attempt", 0)) != int(document["run_attempt"])):
        raise ContractError("HEAD_UNAVAILABLE", "workflow not successful main attempt")
    expected_identity = (
        f"run:{document['run_id']}:attempt:{document['run_attempt']}:operational_head"
    )
    if document.get("identity") != expected_identity:
        raise ContractError("MALFORMED_ANCHOR", "head identity")
    if document.get("genesis_identity") != genesis_identity:
        raise ContractError("ANCHOR_FORK", "genesis identity")
    if tuple(document.get("source_allowlist", ())) != INCREMENTAL_SOURCE_ALLOWLIST:
        raise ContractError("MALFORMED_ANCHOR", "source allowlist")
    anchors = {
        int(interval): anchor_from_dict(int(interval), value)
        for interval, value in document["anchors"].items()
    }
    if set(anchors) != set(INTERVALS):
        raise ContractError("MALFORMED_ANCHOR", "head intervals")
    return AnchorBundle(
        identity=str(document["identity"]),
        predecessor_identity=str(document["predecessor_identity"]),
        predecessor_digest=str(document["predecessor_digest"]),
        genesis_identity=str(document["genesis_identity"]),
        run_id=int(document["run_id"]),
        run_attempt=int(document["run_attempt"]),
        source_allowlist=tuple(document["source_allowlist"]),
        anchors=anchors,
        artifact_id=int(document["artifact_id"]) if document.get("artifact_id") else None,
        artifact_digest=document.get("artifact_digest"),
    )


def _bundle_dominates(left: AnchorBundle, right: AnchorBundle) -> bool:
    strictly_ahead = False
    for interval in INTERVALS:
        left_anchor = left.anchors[interval]
        right_anchor = right.anchors[interval]
        if left_anchor.min_ts != right_anchor.min_ts:
            return False
        if (left_anchor.count < right_anchor.count
                or parse_utc(left_anchor.accepted_through_ts)
                < parse_utc(right_anchor.accepted_through_ts)):
            return False
        strictly_ahead = strictly_ahead or (
            left_anchor.count > right_anchor.count
            or parse_utc(left_anchor.accepted_through_ts)
            > parse_utc(right_anchor.accepted_through_ts)
        )
    return strictly_ahead


def select_verified_head(
    db: Any, genesis: AnchorBundle, candidates: Sequence[AnchorBundle],
    *, events: list[str] | None = None,
) -> tuple[AnchorBundle, dict[int, list[dict[str, Any]]]]:
    """Select one fully DB-verified live head without requiring expired ancestors."""
    if not candidates:
        return genesis, verify_bundle_prefixes(db, genesis, events=events)
    by_identity = {genesis.identity: genesis}
    for candidate in candidates:
        if candidate.identity in by_identity:
            raise ContractError("ANCHOR_FORK", "duplicate head identity")
        if candidate.genesis_identity != genesis.identity:
            raise ContractError("ANCHOR_FORK", "genesis identity")
        for interval in INTERVALS:
            genesis_anchor = genesis.anchors[interval]
            candidate_anchor = candidate.anchors[interval]
            if (candidate_anchor.min_ts != genesis_anchor.min_ts
                    or candidate_anchor.count < genesis_anchor.count
                    or parse_utc(candidate_anchor.accepted_through_ts)
                    < parse_utc(genesis_anchor.accepted_through_ts)):
                raise ContractError("ANCHOR_FORK", "head precedes genesis")
        by_identity[candidate.identity] = candidate

    verified: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for candidate in candidates:
        predecessor = by_identity.get(candidate.predecessor_identity)
        if (predecessor is not None
                and candidate.predecessor_digest
                != combined_anchor_digest(predecessor.anchors)):
            raise ContractError("ANCHOR_FORK", "predecessor digest")
        verified[candidate.identity] = verify_bundle_prefixes(
            db, candidate, events=events
        )

    maximal = [
        candidate for candidate in candidates
        if not any(
            other.identity != candidate.identity
            and _bundle_dominates(other, candidate)
            for other in candidates
        )
    ]
    if len(maximal) != 1:
        raise ContractError("ANCHOR_FORK", "no unique verified live descendant")
    selected = maximal[0]
    live_ancestors = {selected.identity}
    cursor = selected
    while cursor.predecessor_identity in by_identity:
        cursor = by_identity[str(cursor.predecessor_identity)]
        if cursor.identity == genesis.identity:
            break
        if cursor.identity in live_ancestors:
            raise ContractError("ANCHOR_FORK", "head cycle")
        live_ancestors.add(cursor.identity)
    if live_ancestors != {candidate.identity for candidate in candidates}:
        raise ContractError("ANCHOR_FORK", "conflicting live descendant")
    return selected, verified[selected.identity]


def combined_anchor_digest(anchors: Mapping[int, Anchor]) -> str:
    payload = {
        str(interval): {
            "min_ts": anchor.min_ts,
            "accepted_through_ts": anchor.accepted_through_ts,
            "count": anchor.count,
            "cumulative_digest": anchor.cumulative_digest,
        }
        for interval, anchor in sorted(anchors.items())
    }
    return sha256_hex(canonical_document(payload))


def build_anchor_document(
    *, kind: str, run_id: int, run_attempt: int, predecessor: AnchorBundle | None,
    manifest_identity: Mapping[str, Any] | None,
    rows_by_interval: Mapping[int, Sequence[Mapping[str, Any]]],
    genesis_identity: str | None,
) -> dict[str, Any]:
    if kind not in ("candidate_genesis", "operational_head"):
        raise ValueError(kind)
    anchors: dict[str, Any] = {}
    for interval in INTERVALS:
        rows = sorted((normalize_row(row) for row in rows_by_interval[interval]), key=row_key)
        anchors[str(interval)] = {
            "min_ts": rows[0]["ts"],
            "accepted_through_ts": rows[-1]["ts"],
            "count": len(rows),
            "cumulative_digest": rowset_digest(rows),
        }
    identity = f"run:{run_id}:attempt:{run_attempt}:{kind}"
    body: dict[str, Any] = {
        "schema_version": ANCHOR_SCHEMA_VERSION,
        "kind": kind,
        "identity": identity,
        "genesis_identity": identity if kind == "candidate_genesis" else genesis_identity,
        "predecessor_identity": predecessor.identity if predecessor else "state-a-legacy",
        "predecessor_digest": (combined_anchor_digest(predecessor.anchors)
                               if predecessor else "state-a-legacy"),
        "run_id": run_id,
        "run_attempt": run_attempt,
        "pre_write_manifest_identity": manifest_identity,
        "source_allowlist": list(
            CATCHUP_SOURCE_ALLOWLIST if kind == "candidate_genesis"
            else INCREMENTAL_SOURCE_ALLOWLIST
        ),
        "anchors": anchors,
        "verification": "full-reread-market-match-continuity-pass",
    }
    return seal_document(body, "head_sha256")


def _json_loads(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("MALFORMED_JSON_RESPONSE") from exc


class SupabaseClient:
    """Minimal PostgREST client using only the service-role apikey header."""

    def __init__(self, url: str, service_role_key: str):
        if not url or not service_role_key:
            raise ContractError("MISSING_SERVICE_ROLE_PRINCIPAL")
        self.base = url.rstrip("/") + "/rest/v1"
        self.key = service_role_key

    def _request(
        self, method: str, path: str, *, body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        data = None if body is None else json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request_headers = {
            "Accept": "application/json",
            "apikey": self.key,
        }
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            self.base + path, data=data, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                parsed = None if not raw else _json_loads(raw)
                return parsed, dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error = ContractError("SUPABASE_HTTP_ERROR", f"{exc.code}: {detail}")
            error.http_status = exc.code  # type: ignore[attr-defined]
            raise error from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ContractError("SUPABASE_TRANSPORT_ERROR", str(exc)) from exc

    @staticmethod
    def _content_total(headers: Mapping[str, str]) -> int:
        content_range = next(
            (value for key, value in headers.items() if key.lower() == "content-range"),
            "",
        )
        match = re.fullmatch(r"(?:\d+-\d+|\*)/(\d+)", content_range)
        if not match:
            raise ContractError("INCOMPLETE_DB_READ", "missing exact Content-Range")
        return int(match.group(1))

    def read_interval(
        self, interval_minutes: int, *, min_ts: str | None = None,
        max_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read every matching row, proving pagination against exact count."""
        rows: list[dict[str, Any]] = []
        total: int | None = None
        offset = 0
        while total is None or offset < total:
            params: list[tuple[str, str]] = [
                ("select", "id,pair,interval_minutes,ts,open,high,low,close,vwap,volume,trade_count,source,created_at"),
                ("pair", f"eq.{PAIR}"),
                ("interval_minutes", f"eq.{interval_minutes}"),
                ("order", "ts.asc,id.asc"),
                ("limit", str(PAGE_SIZE)),
                ("offset", str(offset)),
            ]
            if min_ts is not None:
                params.append(("ts", f"gte.{canonical_ts(min_ts)}"))
            if max_ts is not None:
                params.append(("ts", f"lte.{canonical_ts(max_ts)}"))
            payload, headers = self._request(
                "GET", f"/{TABLE}?{urllib.parse.urlencode(params)}",
                headers={"Prefer": "count=exact"},
            )
            if not isinstance(payload, list):
                raise ContractError("INCOMPLETE_DB_READ", "response is not a list")
            page_total = self._content_total(headers)
            if total is None:
                total = page_total
            elif page_total != total:
                raise ContractError("INCOMPLETE_DB_READ", "count changed during pagination")
            if not payload and offset < total:
                raise ContractError("INCOMPLETE_DB_READ", "empty page before exact count")
            rows.extend(payload)
            offset += len(payload)
            if len(payload) == 0:
                break
        if total is None or len(rows) != total:
            raise ContractError("INCOMPLETE_DB_READ", f"read {len(rows)} of {total}")
        return rows

    def insert_batch(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        payload = []
        for original in rows:
            row = normalize_row(original)
            payload.append({field: row[field] for field in CANONICAL_FIELDS})
        self._request(
            "POST", f"/{TABLE}", body=payload,
            headers={"Prefer": "return=minimal"},
        )


class KrakenPublicClient:
    def fetch(self, interval_minutes: int) -> Mapping[str, Any]:
        query = urllib.parse.urlencode({
            "pair": PAIR,
            "interval": str(interval_minutes),
            "since": "0",
        })
        request = urllib.request.Request(
            f"{KRAKEN_OHLC_URL}?{query}",
            headers={"Accept": "application/json", "User-Agent": "dca-bot-ohlc-preserve/1.3"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return _json_loads(response.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            raise ContractError("KRAKEN_FETCH_FAILED", str(exc)) from exc


class GitHubArtifactClient:
    """Read-only GitHub artifact discovery/download client."""

    def __init__(self, repo: str, token: str, api_url: str = "https://api.github.com"):
        if not repo or not token:
            raise ContractError("GITHUB_ARTIFACT_ACCESS_MISSING")
        self.repo = repo
        self.token = token
        self.base = api_url.rstrip("/")

    def _request(self, path: str, *, raw: bool = False) -> Any:
        request = urllib.request.Request(
            self.base + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "dca-bot-ohlc-preserve/1.3",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return body if raw else _json_loads(body)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            raise ContractError("GITHUB_ARTIFACT_READ_FAILED", str(exc)) from exc

    def artifact(self, artifact_id: int) -> Mapping[str, Any]:
        value = self._request(f"/repos/{self.repo}/actions/artifacts/{artifact_id}")
        if not isinstance(value, Mapping):
            raise ContractError("GITHUB_ARTIFACT_READ_FAILED", "metadata")
        return value

    def workflow_run(self, run_id: int, attempt: int | None = None) -> Mapping[str, Any]:
        suffix = (f"/attempts/{attempt}" if attempt is not None else "")
        value = self._request(f"/repos/{self.repo}/actions/runs/{run_id}{suffix}")
        if not isinstance(value, Mapping):
            raise ContractError("GITHUB_ARTIFACT_READ_FAILED", "workflow run")
        return value

    def list_artifacts(self, *, run_id: int | None = None) -> list[Mapping[str, Any]]:
        artifacts: list[Mapping[str, Any]] = []
        page = 1
        while True:
            root = (f"/repos/{self.repo}/actions/runs/{run_id}/artifacts"
                    if run_id is not None else f"/repos/{self.repo}/actions/artifacts")
            value = self._request(f"{root}?per_page=100&page={page}")
            batch = value.get("artifacts") if isinstance(value, Mapping) else None
            if not isinstance(batch, list):
                raise ContractError("GITHUB_ARTIFACT_READ_FAILED", "artifact list")
            artifacts.extend(item for item in batch if isinstance(item, Mapping))
            if len(batch) < 100:
                return artifacts
            page += 1

    def download_json(self, metadata: Mapping[str, Any], expected_filename: str) -> Mapping[str, Any]:
        artifact_id = int(metadata["id"])
        archive = self._request(
            f"/repos/{self.repo}/actions/artifacts/{artifact_id}/zip", raw=True
        )
        claimed = str(metadata.get("digest", "")).removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", claimed) or sha256_hex(archive) != claimed:
            raise ContractError("ARTIFACT_DIGEST_MISMATCH", str(artifact_id))
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                names = [name for name in bundle.namelist() if not name.endswith("/")]
                if names != [expected_filename]:
                    raise ContractError("ARTIFACT_IDENTITY_MISMATCH", repr(names))
                data = bundle.read(expected_filename)
        except zipfile.BadZipFile as exc:
            raise ContractError("ARTIFACT_DIGEST_MISMATCH", "invalid zip") from exc
        value = _json_loads(data)
        if not isinstance(value, Mapping):
            raise ContractError("ARTIFACT_IDENTITY_MISMATCH", "document")
        return value

    @staticmethod
    def _validate_run(run: Mapping[str, Any], *, run_id: int, attempt: int,
                      require_success: bool) -> None:
        if (int(run.get("id", -1)) != run_id
                or int(run.get("run_attempt", -1)) != attempt
                or run.get("head_branch") != "main"
                or run.get("path") != WORKFLOW_PATH):
            raise ContractError("ARTIFACT_IDENTITY_MISMATCH", "workflow authority")
        if require_success and run.get("conclusion") != "success":
            raise ContractError("HEAD_UNAVAILABLE", "workflow conclusion")

    @staticmethod
    def _validate_manifest_run(
        manifest: Mapping[str, Any], run: Mapping[str, Any],
    ) -> None:
        if manifest.get("execution_commit_sha") != run.get("head_sha"):
            raise ContractError("EXECUTION_PROVENANCE_MISMATCH", "workflow head_sha")

    def load_resume_manifest(
        self, run_id: int, attempt: int, family: str,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        candidates = []
        pattern = re.compile(
            rf"ohlc-prewrite-{run_id}-{attempt}-{re.escape(family)}-(?:apply|resume)$"
        )
        for artifact in self.list_artifacts(run_id=run_id):
            if pattern.fullmatch(str(artifact.get("name", ""))) and not artifact.get("expired"):
                candidates.append(artifact)
        if len(candidates) != 1:
            raise ContractError("MANIFEST_NOT_FOUND", f"found {len(candidates)}")
        run = self.workflow_run(run_id, attempt)
        self._validate_run(run, run_id=run_id, attempt=attempt, require_success=False)
        artifact = candidates[0]
        if not artifact_retention_ok(str(artifact.get("created_at")),
                                     str(artifact.get("expires_at"))):
            raise ContractError("ARTIFACT_RETENTION_TOO_SHORT")
        filename = f"prewrite-{run_id}-{attempt}.json"
        manifest = self.download_json(artifact, filename)
        validate_manifest(manifest, run_id=run_id, run_attempt=attempt)
        self._validate_manifest_run(manifest, run)
        if not str(manifest.get("mode", "")).startswith(family):
            raise ContractError("MANIFEST_MISMATCH", "mode family")
        return manifest, artifact

    def load_all_manifests(self) -> list[Mapping[str, Any]]:
        manifests: list[Mapping[str, Any]] = []
        for artifact in self.list_artifacts():
            name = str(artifact.get("name", ""))
            match = re.fullmatch(r"ohlc-prewrite-(\d+)-(\d+)-.+", name)
            if not match or artifact.get("expired"):
                continue
            if not artifact_retention_ok(str(artifact.get("created_at")),
                                         str(artifact.get("expires_at"))):
                raise ContractError("ARTIFACT_RETENTION_TOO_SHORT", name)
            run_id, attempt = int(match.group(1)), int(match.group(2))
            run = self.workflow_run(run_id, attempt)
            self._validate_run(run, run_id=run_id, attempt=attempt, require_success=False)
            manifest = self.download_json(artifact, f"prewrite-{run_id}-{attempt}.json")
            validate_manifest(manifest, run_id=run_id, run_attempt=attempt)
            self._validate_manifest_run(manifest, run)
            expected_name = (
                f"ohlc-prewrite-{run_id}-{attempt}-{manifest['mode']}"
            )
            if name != expected_name:
                raise ContractError("ARTIFACT_IDENTITY_MISMATCH", name)
            manifests.append(manifest)
        return manifests

    def load_head_candidates(self, genesis: AnchorBundle) -> list[AnchorBundle]:
        candidates: list[AnchorBundle] = []
        for artifact in self.list_artifacts():
            name = str(artifact.get("name", ""))
            match = re.fullmatch(r"ohlc-operational-head-(\d+)-(\d+)", name)
            if not match or artifact.get("expired"):
                continue
            if not artifact_retention_ok(str(artifact.get("created_at")),
                                         str(artifact.get("expires_at"))):
                raise ContractError("ARTIFACT_RETENTION_TOO_SHORT", name)
            run_id, attempt = int(match.group(1)), int(match.group(2))
            run = self.workflow_run(run_id, attempt)
            if run.get("conclusion") != "success":
                continue
            self._validate_run(run, run_id=run_id, attempt=attempt, require_success=True)
            document = self.download_json(artifact, f"operational-head-{run_id}-{attempt}.json")
            candidate = accept_head_candidate(
                document, workflow_run=run, genesis_identity=genesis.identity
            )
            candidate = AnchorBundle(
                identity=candidate.identity,
                predecessor_identity=candidate.predecessor_identity,
                predecessor_digest=candidate.predecessor_digest,
                genesis_identity=candidate.genesis_identity,
                run_id=candidate.run_id,
                run_attempt=candidate.run_attempt,
                source_allowlist=candidate.source_allowlist,
                anchors=candidate.anchors,
                artifact_id=int(artifact["id"]),
                artifact_digest=str(artifact.get("digest", "")).removeprefix("sha256:"),
            )
            candidates.append(candidate)
        return candidates


def _normalized_snapshot(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted((normalize_row(row) for row in rows), key=row_key)


def legacy_bundle(rows_by_interval: Mapping[int, Sequence[Mapping[str, Any]]]) -> AnchorBundle:
    anchors = {}
    for interval in INTERVALS:
        rows = _normalized_snapshot(rows_by_interval[interval])
        anchors[interval] = Anchor(
            interval_minutes=interval,
            min_ts=rows[0]["ts"],
            accepted_through_ts=rows[-1]["ts"],
            count=len(rows),
            cumulative_digest=rowset_digest(rows),
        )
    return AnchorBundle(
        identity="state-a-legacy",
        predecessor_identity=None,
        predecessor_digest=None,
        genesis_identity="UNINITIALIZED",
        run_id=0,
        run_attempt=0,
        source_allowlist=(LEGACY_SOURCE,),
        anchors=anchors,
    )


def verify_manifest_predecessor(
    manifest: Mapping[str, Any], predecessor: AnchorBundle,
) -> None:
    if (manifest.get("predecessor_identity") != predecessor.identity
            or manifest.get("predecessor_cumulative_digest")
            != combined_anchor_digest(predecessor.anchors)):
        raise ContractError("ANCHOR_FORK", "resume manifest predecessor")


def target_minimum(interval: int, daily_target_min_ts: str) -> str:
    return (daily_target_min_ts
            if interval == DAILY_INTERVAL else FOUR_HOUR_TARGET_MIN_TS)


def intended_source_for(mode: str, genesis: AnchorBundle | None) -> str:
    if mode.startswith("catch-up") or (mode == "dry-run" and genesis is None):
        return CATCHUP_SOURCE
    return INCREMENTAL_SOURCE


def source_allowlist_for(mode: str, genesis: AnchorBundle | None) -> tuple[str, ...]:
    return (CATCHUP_SOURCE_ALLOWLIST
            if intended_source_for(mode, genesis) == CATCHUP_SOURCE
            else INCREMENTAL_SOURCE_ALLOWLIST)


def bundle_to_dict(bundle: AnchorBundle) -> dict[str, Any]:
    return {
        "identity": bundle.identity,
        "predecessor_identity": bundle.predecessor_identity,
        "predecessor_digest": bundle.predecessor_digest,
        "genesis_identity": bundle.genesis_identity,
        "run_id": bundle.run_id,
        "run_attempt": bundle.run_attempt,
        "source_allowlist": list(bundle.source_allowlist),
        "artifact_id": bundle.artifact_id,
        "artifact_digest": bundle.artifact_digest,
        "anchors": {
            str(interval): {
                "min_ts": anchor.min_ts,
                "accepted_through_ts": anchor.accepted_through_ts,
                "count": anchor.count,
                "cumulative_digest": anchor.cumulative_digest,
            }
            for interval, anchor in bundle.anchors.items()
        },
    }


def bundle_from_dict(value: Mapping[str, Any]) -> AnchorBundle:
    anchors = {
        int(interval): anchor_from_dict(int(interval), anchor)
        for interval, anchor in value["anchors"].items()
    }
    return AnchorBundle(
        identity=str(value["identity"]),
        predecessor_identity=value.get("predecessor_identity"),
        predecessor_digest=value.get("predecessor_digest"),
        genesis_identity=str(value["genesis_identity"]),
        run_id=int(value["run_id"]),
        run_attempt=int(value["run_attempt"]),
        source_allowlist=tuple(value["source_allowlist"]),
        anchors=anchors,
        artifact_id=(int(value["artifact_id"]) if value.get("artifact_id") else None),
        artifact_digest=value.get("artifact_digest"),
    )


def discover_lifecycle_rows(db: Any, events: list[str] | None = None) -> dict[int, list[dict[str, Any]]]:
    """The mandatory first DB operation for lifecycle determination."""
    rows = {interval: db.read_interval(interval) for interval in INTERVALS}
    if events is not None:
        events.append("lifecycle_db_discovery")
    for interval in INTERVALS:
        if not rows[interval]:
            raise ContractError("SILENT_EMPTY_DB_READ", str(interval))
        if duplicate_keys(rows[interval]):
            raise ContractError("DB_DUPLICATE_KEYS", str(interval))
    return rows


def verify_bundle_prefixes(
    db: Any, bundle: AnchorBundle, *, events: list[str] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    prefixes: dict[int, list[dict[str, Any]]] = {}
    for interval in INTERVALS:
        anchor = bundle.anchors[interval]
        rows = db.read_interval(
            interval, min_ts=anchor.min_ts, max_ts=anchor.accepted_through_ts
        )
        verify_anchor_prefix(anchor, rows, bundle.source_allowlist)
        prefixes[interval] = rows
        if events is not None:
            events.append(f"anchor_prefix_{interval}")
    return prefixes


def _rows_after_anchor(
    rows: Sequence[Mapping[str, Any]], anchor: Anchor,
) -> list[Mapping[str, Any]]:
    through = parse_utc(anchor.accepted_through_ts)
    return [row for row in rows if parse_utc(row["ts"]) > through]


def resolve_lifecycle(
    *, mode: str, confirmation: Confirmation, db: Any,
    github: Any | None, genesis: AnchorBundle | None,
    events: list[str] | None = None,
) -> dict[str, Any]:
    """Discover DB state first, then select A/B/C without guessing."""
    discovered = discover_lifecycle_rows(db, events)
    resume_manifest = None
    resume_artifact = None
    if mode in RESUME_MODES:
        if github is None:
            raise ContractError("GITHUB_ARTIFACT_ACCESS_MISSING")
        family = "catch-up" if mode.startswith("catch-up") else "incremental"
        resume_manifest, resume_artifact = github.load_resume_manifest(
            int(confirmation.run_id), int(confirmation.run_attempt), family
        )

    if genesis is None:
        if mode.startswith("incremental") or mode == "head-rebuild":
            raise ContractError("MISSING_GENESIS")
        if mode == "catch-up-resume":
            validate_state_b(discovered, resume_manifest, "catch-up")
            state = "B"
        else:
            validate_state_a(discovered)
            state = "A"
        accepted = legacy_bundle({
            interval: [row for row in discovered[interval]
                       if row.get("source") == LEGACY_SOURCE]
            for interval in INTERVALS
        })
        if resume_manifest is not None:
            verify_manifest_predecessor(resume_manifest, accepted)
        prefixes = {
            interval: [row for row in discovered[interval]
                       if row.get("source") == LEGACY_SOURCE]
            for interval in INTERVALS
        }
        return {
            "state": state,
            "discovered": discovered,
            "accepted": accepted,
            "prefixes": prefixes,
            "resume_manifest": resume_manifest,
            "resume_artifact": resume_artifact,
            "historic_manifests": [resume_manifest] if resume_manifest else [],
        }

    if mode.startswith("catch-up"):
        raise ContractError("BASELINE_CONFLICT", "genesis is already initialized")

    # Static genesis is always verified from a complete, separately paginated
    # prefix before any operational head is considered.
    genesis_prefixes = verify_bundle_prefixes(db, genesis, events=events)
    candidates = github.load_head_candidates(genesis) if github is not None else []
    if candidates:
        accepted, prefixes = select_verified_head(
            db, genesis, candidates, events=events
        )
    else:
        accepted, prefixes = genesis, genesis_prefixes
    if resume_manifest is not None:
        verify_manifest_predecessor(resume_manifest, accepted)

    if mode == "head-rebuild":
        if accepted.identity != genesis.identity:
            raise ContractError("ANCHOR_FORK", "a valid operational head already exists")
        historic = github.load_all_manifests() if github is not None else []
        state = "C"
    elif mode == "incremental-resume":
        historic = [resume_manifest]
        state = "B"
    else:
        historic = []
        state = "C"
        if any(_rows_after_anchor(discovered[interval], accepted.anchors[interval])
               for interval in INTERVALS):
            raise ContractError("HEAD_UNAVAILABLE", "DB extends beyond accepted head")

    return {
        "state": state,
        "discovered": discovered,
        "accepted": accepted,
        "prefixes": prefixes,
        "resume_manifest": resume_manifest,
        "resume_artifact": resume_artifact,
        "historic_manifests": historic,
    }


def plan_interval(
    *, interval: int, run_started_at: str, db_rows: Sequence[Mapping[str, Any]],
    accepted_prefix: Sequence[Mapping[str, Any]], accepted_anchor: Anchor,
    fetch: FetchResult, lifecycle_state: str, intended_source: str,
    source_allowlist: Sequence[str], daily_target_min_ts: str,
    historic_manifests: Sequence[Mapping[str, Any]],
    read_only_rebuild: bool = False,
) -> dict[str, Any]:
    if fetch.duplicate_keys:
        raise ContractError("FETCHED_DUPLICATE_KEYS", str(fetch.duplicate_keys))
    if not fetch.committed:
        raise ContractError("KRAKEN_RESPONSE_ERROR", f"{interval} no committed rows")
    expected_latest = expected_latest_committed_ts(run_started_at, interval)
    recoverable_min = fetch.committed[0]["ts"]
    committed_max = fetch.committed[-1]["ts"]
    fetched = {row_key(row): row for row in fetch.committed}
    accepted_keys = {row_key(row) for row in accepted_prefix}
    classification = classify_existing_rows(
        db_rows,
        accepted_keys=accepted_keys,
        fetched_rows=fetched,
        historic_manifests=historic_manifests,
        source_allowlist=source_allowlist,
    )
    if classification["mismatched_existing"]:
        raise ContractError(
            "MISMATCHED_EXISTING", str(classification["mismatched_existing"][0])
        )
    if classification["unverifiable_historic"]:
        raise ContractError(
            "UNVERIFIABLE_HISTORIC",
            str(classification["unverifiable_historic"][0]),
        )

    start = target_minimum(interval, daily_target_min_ts)
    db_keys = {row_key(row) for row in db_rows}
    if read_only_rebuild:
        db_max = max(canonical_ts(row["ts"]) for row in db_rows)
        if parse_utc(db_max) > parse_utc(expected_latest):
            raise ContractError("UNCOMMITTED_DB_ROW", db_max)
        continuity_end = db_max
        would_insert: list[Mapping[str, Any]] = []
    else:
        continuity_end = expected_latest
        would_insert = [
            row for row in fetch.committed
            if parse_utc(start) <= parse_utc(row["ts"]) <= parse_utc(expected_latest)
            and row_key(row) not in db_keys
        ]
    existing_normalized = _normalized_snapshot(db_rows)
    prospective = existing_normalized + [normalize_row(row) for row in would_insert]
    gaps_before = continuity_gaps(db_rows, start, continuity_end, interval)
    gaps_after = continuity_gaps(prospective, start, continuity_end, interval)
    gap_kind = gap_classification(gaps_after, recoverable_min)
    if gaps_after:
        raise ContractError(gap_kind, gaps_after[0])
    scoped_prospective = [
        row for row in prospective
        if parse_utc(start) <= parse_utc(row["ts"]) <= parse_utc(continuity_end)
    ]
    scoped_prospective.sort(key=row_key)
    if not scoped_prospective:
        raise ContractError("CONTINUITY_FAILURE", str(interval))
    anchor_start = min(
        parse_utc(start), parse_utc(accepted_anchor.min_ts)
    )
    anchor_start_ts = canonical_ts(anchor_start)
    anchor_gaps = continuity_gaps(
        prospective, anchor_start_ts, continuity_end, interval
    )
    if anchor_gaps:
        raise ContractError(
            gap_classification(anchor_gaps, recoverable_min), anchor_gaps[0]
        )
    anchor_rows = [
        row for row in prospective
        if anchor_start <= parse_utc(row["ts"]) <= parse_utc(continuity_end)
    ]
    anchor_rows.sort(key=row_key)
    return {
        "interval_minutes": interval,
        "lifecycle_state": lifecycle_state,
        "daily_target_min_ts": daily_target_min_ts,
        "fetched_total": fetch.total,
        "rejected_current": len(fetch.rejected_current),
        "fetched_committed": len(fetch.committed),
        "recoverable_minimum": recoverable_min,
        "committed_maximum": committed_max,
        "expected_latest_committed_ts": expected_latest,
        "existing_count": len(db_rows),
        "existing_min": min(canonical_ts(row["ts"]) for row in db_rows),
        "existing_max": max(canonical_ts(row["ts"]) for row in db_rows),
        "accepted_anchor_count": accepted_anchor.count,
        "accepted_anchor_through": accepted_anchor.accepted_through_ts,
        "accepted_anchor_digest": accepted_anchor.cumulative_digest,
        "full_prefix_rows_read": len(accepted_prefix),
        "full_prefix_complete": len(accepted_prefix) == accepted_anchor.count,
        "matching_existing": classification["matching_existing"],
        "mismatched_existing": len(classification["mismatched_existing"]),
        "would_insert": len(would_insert),
        "fetched_duplicates": len(fetch.duplicate_keys),
        "db_duplicates": len(duplicate_keys(db_rows)),
        "recoverable_verifiable": classification["recoverable_verifiable"],
        "manifest_verifiable_historic": classification["manifest_verifiable_historic"],
        "unverifiable_historic": len(classification["unverifiable_historic"]),
        "gaps_before": gap_summary(gaps_before),
        "prospective_gaps_after": gap_summary(gaps_after),
        "prospective_min": scoped_prospective[0]["ts"],
        "prospective_max": scoped_prospective[-1]["ts"],
        "gap_classification": gap_kind,
        "intended_source": intended_source,
        "prospective_would_insert_rowset_sha256": rowset_digest(would_insert),
        "would_insert_rows": [normalize_row(row) for row in would_insert],
        "prospective_rows": anchor_rows,
    }


def build_plans(
    *, lifecycle: Mapping[str, Any], fetches: Mapping[int, FetchResult],
    run_started_at: str, intended_source: str, source_allowlist: Sequence[str],
    mode: str, daily_target_min_ts: str,
) -> dict[int, dict[str, Any]]:
    accepted: AnchorBundle = lifecycle["accepted"]
    return {
        interval: plan_interval(
            interval=interval,
            run_started_at=run_started_at,
            db_rows=lifecycle["discovered"][interval],
            accepted_prefix=lifecycle["prefixes"][interval],
            accepted_anchor=accepted.anchors[interval],
            fetch=fetches[interval],
            lifecycle_state=lifecycle["state"],
            intended_source=intended_source,
            source_allowlist=source_allowlist,
            daily_target_min_ts=daily_target_min_ts,
            historic_manifests=lifecycle["historic_manifests"],
            read_only_rebuild=mode == "head-rebuild",
        )
        for interval in INTERVALS
    }


def public_report(plans: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    hidden = {"would_insert_rows", "prospective_rows"}
    return {
        str(interval): {key: value for key, value in plan.items() if key not in hidden}
        for interval, plan in plans.items()
    }


def snapshot_digest(rows_by_interval: Mapping[int, Sequence[Mapping[str, Any]]]) -> str:
    rows = [
        normalize_row(row)
        for interval in INTERVALS
        for row in rows_by_interval[interval]
    ]
    return rowset_digest(rows)


def validate_prewrite_snapshot(
    expected_rows: Mapping[int, Sequence[Mapping[str, Any]]],
    current_rows: Mapping[int, Sequence[Mapping[str, Any]]],
    intended_rows: Sequence[Mapping[str, Any]],
) -> None:
    intended = {row_key(row): row for row in intended_rows}
    for interval in INTERVALS:
        expected = {row_key(row): normalize_row(row) for row in expected_rows[interval]}
        current = {row_key(row): normalize_row(row) for row in current_rows[interval]}
        if len(current) != len(current_rows[interval]):
            raise ContractError("DB_DUPLICATE_KEYS", str(interval))
        for key, row in expected.items():
            if key not in current or canonical_json_line(current[key]) != canonical_json_line(row):
                raise ContractError("DB_CHANGED_SINCE_PREFLIGHT", str(key))
        for key, row in current.items():
            if key in expected:
                continue
            if key not in intended:
                raise ContractError("DB_CHANGED_SINCE_PREFLIGHT", str(key))
            verify_race(row, intended[key])


def insert_with_race_readback(
    db: Any, interval: int, rows: Sequence[Mapping[str, Any]], *, chunk_size: int = 100,
) -> None:
    for offset in range(0, len(rows), chunk_size):
        remaining = list(rows[offset:offset + chunk_size])
        attempts = 0
        while remaining:
            attempts += 1
            try:
                db.insert_batch(remaining)
                break
            except ContractError as exc:
                if getattr(exc, "http_status", None) != 409 or attempts > 3:
                    raise
                reread = {row_key(row): row for row in db.read_interval(interval)}
                next_remaining = []
                for intended in remaining:
                    status = verify_race(reread.get(row_key(intended)), intended)
                    if status == "missing":
                        next_remaining.append(intended)
                if len(next_remaining) == len(remaining):
                    raise
                remaining = next_remaining


def verify_intended_readback(
    rows: Sequence[Mapping[str, Any]], intended: Sequence[Mapping[str, Any]],
) -> None:
    actual = {row_key(row): row for row in rows}
    for expected in intended:
        key = row_key(expected)
        if key not in actual:
            raise ContractError("POST_WRITE_REREAD_MISSING", str(key))
        if (actual[key].get("source") != expected.get("source")
                or not market_fields_equal(actual[key], expected)):
            raise ContractError("POST_WRITE_REREAD_MISMATCH", str(key))


def _github_output(values: Mapping[str, Any]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        for key, value in values.items():
            handle.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def _write_document(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_document(value))


def _read_document(path: Path) -> Mapping[str, Any]:
    value = _json_loads(path.read_bytes())
    if not isinstance(value, Mapping):
        raise ContractError("MALFORMED_STATE")
    return value


def prepare_run(
    *, mode: str, confirmation_text: str, run_started_at: str,
    run_id: int, run_attempt: int, db: Any, kraken: Any,
    github: Any | None, genesis: AnchorBundle | None, state_dir: Path,
    execution_commit_sha: str, daily_target_min_ts: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", execution_commit_sha):
        raise ContractError("INVALID_EXECUTION_COMMIT_SHA", execution_commit_sha)
    daily_target_min_ts = validate_daily_target(daily_target_min_ts)
    confirmation = parse_confirmation(mode, confirmation_text)
    wait_until = boundary_settlement(run_started_at)
    if wait_until is not None:
        raise ContractError("BOUNDARY_SETTLEMENT_WAIT", wait_until)

    events: list[str] = []
    lifecycle = resolve_lifecycle(
        mode=mode, confirmation=confirmation, db=db, github=github,
        genesis=genesis, events=events,
    )
    source = intended_source_for(mode, genesis)
    allowlist = source_allowlist_for(mode, genesis)
    fetches = {
        interval: parse_kraken_payload(
            kraken.fetch(interval), interval, run_started_at, intended_source=source
        )
        for interval in INTERVALS
    }
    plans = build_plans(
        lifecycle=lifecycle, fetches=fetches, run_started_at=run_started_at,
        intended_source=source, source_allowlist=allowlist, mode=mode,
        daily_target_min_ts=daily_target_min_ts,
    )
    report = public_report(plans)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if mode == "dry-run":
        _github_output({"needs_prewrite": False, "anchor_ready": False})
        return {"report": report, "events": events}

    if mode == "head-rebuild":
        rows_by_interval = {
            interval: plans[interval]["prospective_rows"] for interval in INTERVALS
        }
        document = build_anchor_document(
            kind="operational_head", run_id=run_id, run_attempt=run_attempt,
            predecessor=genesis, manifest_identity=None,
            rows_by_interval=rows_by_interval,
            genesis_identity=genesis.identity if genesis else None,
        )
        filename = f"operational-head-{run_id}-{run_attempt}.json"
        path = state_dir / filename
        _write_document(path, document)
        name = f"ohlc-operational-head-{run_id}-{run_attempt}"
        _github_output({
            "needs_prewrite": False,
            "anchor_ready": True,
            "anchor_path": str(path),
            "anchor_name": name,
            "anchor_kind": "operational_head",
        })
        return {"report": report, "events": events, "anchor": document}

    intended_rows = [
        row
        for interval in INTERVALS
        for row in plans[interval]["would_insert_rows"]
    ]
    accepted: AnchorBundle = lifecycle["accepted"]
    recoverable = {
        interval: plans[interval]["recoverable_minimum"] for interval in INTERVALS
    }
    expected_latest = {
        interval: plans[interval]["expected_latest_committed_ts"]
        for interval in INTERVALS
    }
    manifest = build_manifest(
        run_id=run_id,
        run_attempt=run_attempt,
        mode=mode,
        run_started_at=run_started_at,
        execution_commit_sha=execution_commit_sha,
        daily_target_min_ts=daily_target_min_ts,
        predecessor_identity=accepted.identity,
        predecessor_digest=combined_anchor_digest(accepted.anchors),
        expected_latest=expected_latest,
        recoverable_minimum=recoverable,
        intended_source=source,
        rows=intended_rows,
        predecessor_evidence=(
            [(lifecycle["resume_manifest"], lifecycle["resume_artifact"])]
            if lifecycle["resume_manifest"] is not None else []
        ),
    )
    manifest_path = state_dir / f"prewrite-{run_id}-{run_attempt}.json"
    _write_document(manifest_path, manifest)
    state_body: dict[str, Any] = {
        "schema_version": "ohlc-preservation-state-v1.3",
        "run_id": run_id,
        "run_attempt": run_attempt,
        "mode": mode,
        "run_started_at": canonical_ts(run_started_at),
        "execution_commit_sha": execution_commit_sha,
        "daily_target_min_ts": daily_target_min_ts,
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_path": str(manifest_path),
        "accepted": bundle_to_dict(accepted),
        "genesis_identity": genesis.identity if genesis else None,
        "intended_source": source,
        "source_allowlist": list(allowlist),
        "db_snapshot": {
            str(interval): _normalized_snapshot(lifecycle["discovered"][interval])
            for interval in INTERVALS
        },
        "plans": {str(interval): plans[interval] for interval in INTERVALS},
    }
    state = seal_document(state_body, "state_sha256")
    state_path = state_dir / f"state-{run_id}-{run_attempt}.json"
    _write_document(state_path, state)
    artifact_name = f"ohlc-prewrite-{run_id}-{run_attempt}-{mode}"
    _github_output({
        "needs_prewrite": True,
        "anchor_ready": False,
        "manifest_path": str(manifest_path),
        "manifest_name": artifact_name,
        "state_path": str(state_path),
    })
    return {"report": report, "events": events, "manifest": manifest, "state": state}


def apply_run(
    *, state_path: Path, artifact_id: int, artifact_name: str,
    platform_digest: str, db: Any, github: Any, state_dir: Path,
    execution_commit_sha: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", execution_commit_sha):
        raise ContractError("INVALID_EXECUTION_COMMIT_SHA", execution_commit_sha)
    state = _read_document(state_path)
    validate_sealed_document(state, "state_sha256")
    if state.get("schema_version") != "ohlc-preservation-state-v1.3":
        raise ContractError("MALFORMED_STATE", "schema")
    if state.get("execution_commit_sha") != execution_commit_sha:
        raise ContractError("EXECUTION_PROVENANCE_MISMATCH", "state binding")
    daily_target_min_ts = validate_daily_target(state.get("daily_target_min_ts"))
    run_id = int(state["run_id"])
    run_attempt = int(state["run_attempt"])
    mode = str(state["mode"])
    if mode not in APPLY_MODES:
        raise ContractError("INVALID_MODE", mode)
    manifest = _read_document(Path(str(state["manifest_path"])))
    validate_manifest(manifest, run_id=run_id, run_attempt=run_attempt)
    if manifest["manifest_sha256"] != state["manifest_sha256"]:
        raise ContractError("MANIFEST_DIGEST_MISMATCH", "state binding")
    if (manifest["execution_commit_sha"] != execution_commit_sha
            or manifest["daily_target_min_ts"] != daily_target_min_ts):
        raise ContractError("EXECUTION_PROVENANCE_MISMATCH", "manifest state binding")
    expected_name = f"ohlc-prewrite-{run_id}-{run_attempt}-{mode}"
    if artifact_name != expected_name:
        raise ContractError("ARTIFACT_IDENTITY_MISMATCH", artifact_name)
    metadata = github.artifact(artifact_id)
    verify_artifact_metadata(
        metadata, artifact_id=artifact_id, name=artifact_name,
        platform_digest=platform_digest,
    )
    uploaded_manifest = github.download_json(
        metadata, f"prewrite-{run_id}-{run_attempt}.json"
    )
    validate_manifest(
        uploaded_manifest, run_id=run_id, run_attempt=run_attempt
    )
    if uploaded_manifest["manifest_sha256"] != manifest["manifest_sha256"]:
        raise ContractError("MANIFEST_DIGEST_MISMATCH", "uploaded artifact")
    manifest_identity = {
        "artifact_id": artifact_id,
        "artifact_name": artifact_name,
        "platform_digest": platform_digest.removeprefix("sha256:"),
        "manifest_sha256": manifest["manifest_sha256"],
        "created_at": metadata["created_at"],
        "expires_at": metadata["expires_at"],
    }

    expected_snapshot = {
        interval: state["db_snapshot"][str(interval)] for interval in INTERVALS
    }
    current = {interval: db.read_interval(interval) for interval in INTERVALS}
    all_intended = [
        row
        for interval in INTERVALS
        for row in state["plans"][str(interval)]["would_insert_rows"]
    ]
    validate_prewrite_snapshot(expected_snapshot, current, all_intended)

    daily = state["plans"][str(DAILY_INTERVAL)]["would_insert_rows"]
    insert_with_race_readback(db, DAILY_INTERVAL, daily)
    daily_readback = db.read_interval(DAILY_INTERVAL)
    verify_intended_readback(daily_readback, daily)

    four_hour = state["plans"][str(FOUR_HOUR_INTERVAL)]["would_insert_rows"]
    insert_with_race_readback(db, FOUR_HOUR_INTERVAL, four_hour)
    four_hour_readback = db.read_interval(FOUR_HOUR_INTERVAL)
    verify_intended_readback(four_hour_readback, four_hour)

    final_rows = {
        DAILY_INTERVAL: db.read_interval(DAILY_INTERVAL),
        FOUR_HOUR_INTERVAL: db.read_interval(FOUR_HOUR_INTERVAL),
    }
    predecessor = bundle_from_dict(state["accepted"])
    scoped: dict[int, list[dict[str, Any]]] = {}
    for interval in INTERVALS:
        if duplicate_keys(final_rows[interval]):
            raise ContractError("DB_DUPLICATE_KEYS", str(interval))
        plan = state["plans"][str(interval)]
        if plan.get("daily_target_min_ts") != daily_target_min_ts:
            raise ContractError("MALFORMED_STATE", "daily target plan binding")
        start = target_minimum(interval, daily_target_min_ts)
        end = plan["expected_latest_committed_ts"]
        gaps = continuity_gaps(final_rows[interval], start, end, interval)
        if gaps:
            raise ContractError(gap_classification(
                gaps, plan["recoverable_minimum"]
            ), gaps[0])
        anchor_start = min(
            parse_utc(start),
            parse_utc(predecessor.anchors[interval].min_ts),
        )
        anchor_gaps = continuity_gaps(
            final_rows[interval], canonical_ts(anchor_start), end, interval
        )
        if anchor_gaps:
            raise ContractError(gap_classification(
                anchor_gaps, plan["recoverable_minimum"]
            ), anchor_gaps[0])
        scoped[interval] = [
            normalize_row(row) for row in final_rows[interval]
            if anchor_start <= parse_utc(row["ts"]) <= parse_utc(end)
        ]
        scoped[interval].sort(key=row_key)
        if any(row["source"] not in state["source_allowlist"]
               for row in scoped[interval]):
            raise ContractError("SOURCE_NOT_ALLOWED", str(interval))

    kind = ("candidate_genesis" if mode.startswith("catch-up")
            else "operational_head")
    document = build_anchor_document(
        kind=kind,
        run_id=run_id,
        run_attempt=run_attempt,
        predecessor=predecessor,
        manifest_identity=manifest_identity,
        rows_by_interval=scoped,
        genesis_identity=state.get("genesis_identity"),
    )
    stem = ("candidate-genesis" if kind == "candidate_genesis"
            else "operational-head")
    filename = f"{stem}-{run_id}-{run_attempt}.json"
    path = state_dir / filename
    _write_document(path, document)
    artifact_name_out = (
        f"ohlc-candidate-genesis-{run_id}-{run_attempt}"
        if kind == "candidate_genesis"
        else f"ohlc-operational-head-{run_id}-{run_attempt}"
    )
    _github_output({
        "anchor_ready": True,
        "anchor_path": str(path),
        "anchor_name": artifact_name_out,
        "anchor_kind": kind,
    })
    return {"anchor": document, "manifest_identity": manifest_identity}


def verify_uploaded_artifact(
    *, github: Any, artifact_id: int, name: str, platform_digest: str,
) -> Mapping[str, Any]:
    metadata = github.artifact(artifact_id)
    verify_artifact_metadata(
        metadata, artifact_id=artifact_id, name=name,
        platform_digest=platform_digest,
    )
    print(json.dumps({
        "artifact_id": artifact_id,
        "name": name,
        "platform_digest": platform_digest.removeprefix("sha256:"),
        "created_at": metadata["created_at"],
        "expires_at": metadata["expires_at"],
        "retention_days": ARTIFACT_RETENTION_DAYS,
    }, separators=(",", ":")))
    return metadata


def _required_positive_int(env: Mapping[str, str], name: str) -> int:
    value = env.get(name, "")
    if not re.fullmatch(r"[1-9]\d*", value):
        raise ContractError("MISSING_RUNTIME_ID", name)
    return int(value)


def validate_execution_environment(
    env: Mapping[str, str],
) -> RuntimeAuthority:
    repository = env.get("GITHUB_REPOSITORY", "")
    if repository != REPOSITORY:
        raise ContractError("REPOSITORY_MISMATCH", repository)
    ref_name = env.get("GITHUB_REF_NAME", "")
    if ref_name != "main":
        raise ContractError("CURRENT_RUN_BRANCH_MISMATCH", ref_name)
    execution_commit_sha = env.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", execution_commit_sha):
        raise ContractError("INVALID_EXECUTION_COMMIT_SHA", execution_commit_sha)
    return RuntimeAuthority(
        repository=repository,
        ref_name=ref_name,
        execution_commit_sha=execution_commit_sha,
    )


def validate_runtime_constants(
    env: Mapping[str, str],
) -> RuntimeAuthority:
    authority = validate_execution_environment(env)
    daily_target = validate_daily_target(env.get("DAILY_TARGET_MIN_TS"))
    validate_source_allowlists(
        env.get("CATCHUP_SOURCE_ALLOWLIST", ""),
        env.get("INCREMENTAL_SOURCE_ALLOWLIST", ""),
    )
    return RuntimeAuthority(
        repository=authority.repository,
        ref_name=authority.ref_name,
        execution_commit_sha=authority.execution_commit_sha,
        daily_target_min_ts=daily_target,
    )


def prepare_from_environment(
    env: Mapping[str, str], *, run_started_at: str, db: Any, kraken: Any,
    github: Any | None, state_dir: Path,
) -> dict[str, Any]:
    """Production prepare path: validate all current-run authority before I/O."""
    authority = validate_runtime_constants(env)
    mode = env.get("PRESERVE_MODE", "dry-run") or "dry-run"
    confirmation = env.get("PRESERVE_CONFIRMATION", "")
    validate_dispatch_inputs({"mode": mode, "confirmation": confirmation})
    return prepare_run(
        mode=mode,
        confirmation_text=confirmation,
        run_started_at=run_started_at,
        run_id=_required_positive_int(env, "GITHUB_RUN_ID"),
        run_attempt=_required_positive_int(env, "GITHUB_RUN_ATTEMPT"),
        db=db,
        kraken=kraken,
        github=github,
        genesis=parse_genesis_constants(env),
        state_dir=state_dir,
        execution_commit_sha=authority.execution_commit_sha,
        daily_target_min_ts=str(authority.daily_target_min_ts),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--state-dir", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--state-dir", required=True)
    apply.add_argument("--state-path", required=True)
    apply.add_argument("--artifact-id", required=True, type=int)
    apply.add_argument("--artifact-name", required=True)
    apply.add_argument("--artifact-digest", required=True)
    verify = sub.add_parser("verify-artifact")
    verify.add_argument("--artifact-id", required=True, type=int)
    verify.add_argument("--artifact-name", required=True)
    verify.add_argument("--artifact-digest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_started_at = canonical_ts(datetime.now(timezone.utc).replace(microsecond=0))
    try:
        execution_authority = validate_execution_environment(os.environ)
        if args.command == "prepare":
            validate_runtime_constants(os.environ)
        elif args.command == "apply":
            validate_source_allowlists(
                os.environ.get("CATCHUP_SOURCE_ALLOWLIST", ""),
                os.environ.get("INCREMENTAL_SOURCE_ALLOWLIST", ""),
            )
        github = GitHubArtifactClient(
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
        if args.command == "verify-artifact":
            verify_uploaded_artifact(
                github=github,
                artifact_id=args.artifact_id,
                name=args.artifact_name,
                platform_digest=args.artifact_digest,
            )
            return 0

        db = SupabaseClient(
            os.environ.get("SUPABASE_URL", ""),
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        )
        if args.command == "apply":
            apply_run(
                state_path=Path(args.state_path),
                artifact_id=args.artifact_id,
                artifact_name=args.artifact_name,
                platform_digest=args.artifact_digest,
                db=db,
                github=github,
                state_dir=Path(args.state_dir),
                execution_commit_sha=execution_authority.execution_commit_sha,
            )
            return 0

        # Captured once at process entry and carried unchanged through the
        # pre-write state into apply.
        prepare_from_environment(
            os.environ,
            run_started_at=run_started_at,
            db=db,
            kraken=KrakenPublicClient(),
            github=github,
            state_dir=Path(args.state_dir),
        )
        return 0
    except ContractError as exc:
        print(json.dumps({
            "status": "FAILED",
            "code": exc.code,
            "detail": exc.detail,
        }, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
