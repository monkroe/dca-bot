"""Hermetic OHLC Preservation Contract v1.3 coverage.

No test in this file opens a socket.  Runtime clients are exercised through
fixtures/fakes, including write ordering, partial failures, pagination, and
artifact validation.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import traceback
import urllib.parse
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import ohlc_preserve as op  # noqa: E402

EXECUTION_SHA = "1" * 40
OTHER_EXECUTION_SHA = "2" * 40


class Runner:
    def __init__(self, title):
        self.title = title
        self.passed = 0
        self.failed = 0

    def check(self, name, actual, expected):
        if actual == expected:
            self.passed += 1
        else:
            self.failed += 1
            print(f"  FAIL  {name}\n        expected: {expected!r}\n        actual:   {actual!r}")

    def check_true(self, name, value):
        self.check(name, bool(value), True)

    def run(self, tests):
        print(f"{self.title} ({len(tests)} branches)")
        for name, fn in tests:
            try:
                fn(self)
            except Exception:
                self.failed += 1
                print(f"  ERROR {name}")
                traceback.print_exc()
        total = self.passed + self.failed
        status = "OK" if not self.failed else "FAILED"
        print(f"  {status}: {self.passed}/{total} assertions")
        return 0 if not self.failed else 1


def error_code(fn):
    try:
        fn()
    except op.ContractError as exc:
        return exc.code
    return None


def utc_range(start, count, minutes):
    first = op.parse_utc(start)
    return [op.canonical_ts(first + timedelta(minutes=minutes * index))
            for index in range(count)]


def row(interval, ts, source=op.LEGACY_SOURCE, created_at=op.LEGACY_CREATED_AT,
        seed="1"):
    return {
        "id": f"id-{interval}-{ts}",
        "pair": op.PAIR,
        "interval_minutes": interval,
        "ts": ts,
        "open": seed,
        "high": str(Decimal(seed) + Decimal("0.2")),
        "low": str(Decimal(seed) - Decimal("0.1")),
        "close": str(Decimal(seed) + Decimal("0.1")),
        "vwap": str(Decimal(seed) + Decimal("0.05")),
        "volume": "10.5000",
        "trade_count": 7,
        "source": source,
        "created_at": created_at,
    }


def baseline():
    return {
        op.DAILY_INTERVAL: [
            row(op.DAILY_INTERVAL, ts)
            for ts in utc_range(op.STATE_A[op.DAILY_INTERVAL]["min"], 103, 1440)
        ],
        op.FOUR_HOUR_INTERVAL: [
            row(op.FOUR_HOUR_INTERVAL, ts)
            for ts in utc_range(op.STATE_A[op.FOUR_HOUR_INTERVAL]["min"], 626, 240)
        ],
    }


def runtime_env(**overrides):
    env = {
        "GITHUB_REPOSITORY": op.REPOSITORY,
        "GITHUB_REF_NAME": "main",
        "GITHUB_SHA": EXECUTION_SHA,
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        "DAILY_TARGET_MIN_TS": op.DAILY_TARGET_MIN_TS,
        "CATCHUP_SOURCE_ALLOWLIST": ",".join(op.CATCHUP_SOURCE_ALLOWLIST),
        "INCREMENTAL_SOURCE_ALLOWLIST": ",".join(op.INCREMENTAL_SOURCE_ALLOWLIST),
        "PRESERVE_MODE": "dry-run",
        "PRESERVE_CONFIRMATION": "",
    }
    for name in (
        "GENESIS_IDENTITY", "GENESIS_ARTIFACT_ID", "GENESIS_ARTIFACT_NAME",
        "GENESIS_ARTIFACT_DIGEST", "GENESIS_RUN_ID", "GENESIS_RUN_ATTEMPT",
        "GENESIS_1440_MIN_TS", "GENESIS_1440_THROUGH_TS", "GENESIS_1440_COUNT",
        "GENESIS_1440_CUMULATIVE_SHA256", "GENESIS_240_MIN_TS",
        "GENESIS_240_THROUGH_TS", "GENESIS_240_COUNT",
        "GENESIS_240_CUMULATIVE_SHA256",
    ):
        env[name] = "UNINITIALIZED"
    env.update(overrides)
    return env


def kraken_payload(rows):
    raw = []
    for item in rows:
        raw.append([
            int(op.parse_utc(item["ts"]).timestamp()),
            item["open"], item["high"], item["low"], item["close"],
            item["vwap"], item["volume"], item["trade_count"],
        ])
    return {"error": [], "result": {"KASUSD": raw, "last": "unused"}}


def fetch(rows, interval, started, source=op.CATCHUP_SOURCE):
    return op.parse_kraken_payload(
        kraken_payload(rows), interval, started, intended_source=source
    )


class FakeDb:
    def __init__(self, rows_by_interval, fail_interval=None, drop_insert_interval=None):
        self.rows = {key: [dict(item) for item in value]
                     for key, value in rows_by_interval.items()}
        self.fail_interval = fail_interval
        self.drop_insert_interval = drop_insert_interval
        self.reads = []
        self.inserts = []
        self.inserted_keys = []

    def read_interval(self, interval, min_ts=None, max_ts=None):
        self.reads.append((interval, min_ts, max_ts))
        values = list(self.rows.get(interval, []))
        if min_ts:
            values = [item for item in values
                      if op.parse_utc(item["ts"]) >= op.parse_utc(min_ts)]
        if max_ts:
            values = [item for item in values
                      if op.parse_utc(item["ts"]) <= op.parse_utc(max_ts)]
        return [dict(item) for item in values]

    def insert_batch(self, values):
        interval = int(values[0]["interval_minutes"]) if values else None
        self.inserts.append(interval)
        if interval == self.fail_interval:
            raise op.ContractError("TEST_INSERT_FAILURE")
        if interval == self.drop_insert_interval:
            return
        for value in values:
            self.inserted_keys.append(op.row_key(value))
            stored = dict(value)
            stored["id"] = "inserted"
            stored["created_at"] = "2026-09-17T12:00:00Z"
            self.rows[interval].append(stored)


class FakeKraken:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def fetch(self, interval):
        self.calls.append(interval)
        return self.payloads[interval]


class FakeGithub:
    def __init__(self, manifest=None, head_candidates=None, manifests=None):
        self.manifest = manifest
        self.head_candidates = head_candidates or []
        self.manifests = manifests or []

    def load_resume_manifest(self, run_id, attempt, family):
        op.validate_manifest(self.manifest, run_id=run_id, run_attempt=attempt)
        return self.manifest, {
            "id": 55,
            "name": f"ohlc-prewrite-{run_id}-{attempt}-{self.manifest['mode']}",
            "digest": "sha256:" + "a" * 64,
        }

    def load_head_candidates(self, genesis):
        return self.head_candidates

    def load_all_manifests(self):
        return self.manifests

    def artifact(self, artifact_id):
        return {
            "id": artifact_id,
            "name": self.artifact_name,
            "digest": "sha256:" + "a" * 64,
            "expired": False,
            "created_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-04-01T00:00:00Z",
        }

    def download_json(self, metadata, expected_filename):
        return self.uploaded_manifest


def one_row_bundle(rows_by_interval, identity="run:1:attempt:1:candidate_genesis"):
    anchors = {}
    for interval, values in rows_by_interval.items():
        anchors[interval] = op.Anchor(
            interval, values[0]["ts"], values[-1]["ts"], len(values),
            op.rowset_digest(values),
        )
    return op.AnchorBundle(
        identity=identity,
        predecessor_identity=None,
        predecessor_digest=None,
        genesis_identity=identity,
        run_id=1,
        run_attempt=1,
        source_allowlist=op.CATCHUP_SOURCE_ALLOWLIST,
        anchors=anchors,
    )


def operational_bundle(rows_by_interval, *, identity, predecessor_identity,
                       predecessor_digest, genesis_identity, run_id):
    anchors = {}
    for interval, values in rows_by_interval.items():
        anchors[interval] = op.Anchor(
            interval, values[0]["ts"], values[-1]["ts"], len(values),
            op.rowset_digest(values),
        )
    return op.AnchorBundle(
        identity=identity,
        predecessor_identity=predecessor_identity,
        predecessor_digest=predecessor_digest,
        genesis_identity=genesis_identity,
        run_id=run_id,
        run_attempt=1,
        source_allowlist=op.INCREMENTAL_SOURCE_ALLOWLIST,
        anchors=anchors,
    )


def test_boundary_and_commitment(t):
    t.check("daily grid midnight", op.on_grid("2026-07-18T00:00:00Z", 1440), True)
    t.check("daily rejects noon", op.on_grid("2026-07-18T12:00:00Z", 1440), False)
    t.check("4h grid", op.on_grid("2026-07-18T20:00:00Z", 240), True)
    t.check("4h rejects 02:00", op.on_grid("2026-07-18T02:00:00Z", 240), False)
    t.check("settlement minute 4", op.boundary_settlement("2026-07-18T04:04:59Z"),
            "2026-07-18T04:05:00Z")
    t.check("minute 5 allowed", op.boundary_settlement("2026-07-18T04:05:00Z"), None)
    t.check("daily expected latest", op.expected_latest_committed_ts(
        "2026-07-18T13:00:00Z", 1440), "2026-07-17T00:00:00Z")
    t.check("4h expected latest", op.expected_latest_committed_ts(
        "2026-07-18T13:00:00Z", 240), "2026-07-18T08:00:00Z")


def test_current_not_last_and_bad_rows(t):
    committed = row(240, "2026-07-18T08:00:00Z", op.CATCHUP_SOURCE)
    current = row(240, "2026-07-18T12:00:00Z", op.CATCHUP_SOURCE)
    result = fetch([current, committed], 240, "2026-07-18T13:00:00Z")
    t.check("current classified by close time, not position",
            [item["ts"] for item in result.rejected_current],
            ["2026-07-18T12:00:00Z"])
    malformed = {"error": [], "result": {"KASUSD": [[1, "1"]], "last": "x"}}
    t.check("malformed fails", error_code(lambda: op.parse_kraken_payload(
        malformed, 240, "2026-07-18T13:00:00Z", intended_source=op.CATCHUP_SOURCE
    )), "MALFORMED_KRAKEN_ROW")
    bad = row(240, "2026-07-18T09:00:00Z", op.CATCHUP_SOURCE)
    t.check("off-grid fails", error_code(lambda: fetch(
        [bad], 240, "2026-07-18T13:00:00Z"
    )), "OFF_GRID_KRAKEN_ROW")


def test_baseline_fingerprint(t):
    values = baseline()
    op.validate_state_a(values)
    t.check("exact state A", True, True)
    mismatch = baseline()
    mismatch[1440] = mismatch[1440][1:]
    t.check("baseline count mismatch", error_code(lambda: op.validate_state_a(mismatch)),
            "BASELINE_CONFLICT")
    empty = baseline()
    empty[240] = []
    t.check("silent empty read", error_code(lambda: op.validate_state_a(empty)),
            "SILENT_EMPTY_DB_READ")
    source = baseline()
    source[1440][0]["source"] = op.CATCHUP_SOURCE
    t.check("legacy source exact", error_code(lambda: op.validate_state_a(source)),
            "BASELINE_CONFLICT")
    created = baseline()
    created[240][0]["created_at"] = "2026-07-17T13:31:42Z"
    t.check("legacy created exact", error_code(lambda: op.validate_state_a(created)),
            "BASELINE_CONFLICT")


def test_lifecycle_states(t):
    values = baseline()
    db = FakeDb(values)
    events = []
    code = error_code(lambda: op.resolve_lifecycle(
        mode="incremental-apply",
        confirmation=op.parse_confirmation("incremental-apply", "APPLY incremental"),
        db=db, github=None, genesis=None, events=events,
    ))
    t.check("missing genesis", code, "MISSING_GENESIS")
    t.check("DB discovery before state decision", events, ["lifecycle_db_discovery"])

    extra = row(1440, "2026-07-17T00:00:00Z", op.CATCHUP_SOURCE)
    legacy = op.legacy_bundle(values)
    manifest = op.build_manifest(
        run_id=10, run_attempt=2, mode="catch-up-apply",
        run_started_at="2026-07-18T13:00:00Z",
        execution_commit_sha=EXECUTION_SHA,
        daily_target_min_ts=op.DAILY_TARGET_MIN_TS,
        predecessor_identity=legacy.identity,
        predecessor_digest=op.combined_anchor_digest(legacy.anchors),
        expected_latest={1440: extra["ts"], 240: values[240][-1]["ts"]},
        recoverable_minimum={1440: values[1440][0]["ts"], 240: values[240][0]["ts"]},
        intended_source=op.CATCHUP_SOURCE, rows=[extra],
    )
    partial = baseline()
    partial[1440].append(extra)
    state_b = op.resolve_lifecycle(
        mode="catch-up-resume",
        confirmation=op.parse_confirmation(
            "catch-up-resume", "RESUME catch-up RUN_ID=10 RUN_ATTEMPT=2"
        ),
        db=FakeDb(partial), github=FakeGithub(manifest), genesis=None,
    )
    t.check("State B catch-up resume", state_b["state"], "B")

    genesis_rows = {
        1440: [row(1440, op.DAILY_TARGET_MIN_TS, op.CATCHUP_SOURCE)],
        240: [row(240, op.FOUR_HOUR_TARGET_MIN_TS, op.CATCHUP_SOURCE)],
    }
    genesis = one_row_bundle(genesis_rows)
    state_c = op.resolve_lifecycle(
        mode="dry-run", confirmation=op.parse_confirmation("dry-run", ""),
        db=FakeDb(genesis_rows), github=FakeGithub(), genesis=genesis,
    )
    t.check("State C genesis", state_c["state"], "C")

    inc = row(240, "2026-04-04T08:00:00Z", op.INCREMENTAL_SOURCE)
    inc_manifest = op.build_manifest(
        run_id=20, run_attempt=3, mode="incremental-apply",
        run_started_at="2026-04-04T12:05:00Z",
        execution_commit_sha=EXECUTION_SHA,
        daily_target_min_ts=op.DAILY_TARGET_MIN_TS,
        predecessor_identity=genesis.identity,
        predecessor_digest=op.combined_anchor_digest(genesis.anchors),
        expected_latest={1440: op.DAILY_TARGET_MIN_TS, 240: inc["ts"]},
        recoverable_minimum={1440: op.DAILY_TARGET_MIN_TS,
                             240: op.FOUR_HOUR_TARGET_MIN_TS},
        intended_source=op.INCREMENTAL_SOURCE, rows=[inc],
    )
    partial_inc = {1440: list(genesis_rows[1440]),
                   240: list(genesis_rows[240]) + [inc]}
    state_b_inc = op.resolve_lifecycle(
        mode="incremental-resume",
        confirmation=op.parse_confirmation(
            "incremental-resume", "RESUME incremental RUN_ID=20 RUN_ATTEMPT=3"
        ),
        db=FakeDb(partial_inc), github=FakeGithub(inc_manifest), genesis=genesis,
    )
    t.check("State B incremental resume", state_b_inc["state"], "B")
    t.check("incremental forbidden without genesis",
            error_code(lambda: op.resolve_lifecycle(
                mode="incremental-resume",
                confirmation=op.parse_confirmation(
                    "incremental-resume",
                    "RESUME incremental RUN_ID=20 RUN_ATTEMPT=3",
                ),
                db=FakeDb(values), github=FakeGithub(inc_manifest), genesis=None,
            )), "MISSING_GENESIS")


def test_anchor_prefix_and_pagination(t):
    values = [row(240, ts, op.CATCHUP_SOURCE)
              for ts in utc_range("2026-04-04T04:00:00Z", 3, 240)]
    anchor = op.Anchor(240, values[0]["ts"], values[-1]["ts"], 3,
                       op.rowset_digest(values))
    op.verify_anchor_prefix(anchor, values, op.CATCHUP_SOURCE_ALLOWLIST)
    t.check("full prefix accepted", True, True)
    t.check("partial prefix rejected", error_code(lambda: op.verify_anchor_prefix(
        anchor, values[:2], op.CATCHUP_SOURCE_ALLOWLIST
    )), "ACCEPTED_ANCHOR_MISMATCH")
    wrong_count = op.Anchor(240, values[0]["ts"], values[-1]["ts"], 2,
                            op.rowset_digest(values))
    t.check("count mismatch", error_code(lambda: op.verify_anchor_prefix(
        wrong_count, values, op.CATCHUP_SOURCE_ALLOWLIST
    )), "ACCEPTED_ANCHOR_MISMATCH")
    wrong_min = op.Anchor(240, values[1]["ts"], values[-1]["ts"], 3,
                          op.rowset_digest(values))
    t.check("min/through mismatch", error_code(lambda: op.verify_anchor_prefix(
        wrong_min, values, op.CATCHUP_SOURCE_ALLOWLIST
    )), "ACCEPTED_ANCHOR_MISMATCH")
    wrong_digest = op.Anchor(240, values[0]["ts"], values[-1]["ts"], 3, "0" * 64)
    t.check("digest mismatch", error_code(lambda: op.verify_anchor_prefix(
        wrong_digest, values, op.CATCHUP_SOURCE_ALLOWLIST
    )), "ACCEPTED_ANCHOR_MISMATCH")

    old_page_size = op.PAGE_SIZE
    op.PAGE_SIZE = 2
    try:
        client = object.__new__(op.SupabaseClient)
        pages = []

        def request(method, path, **kwargs):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            offset = int(query["offset"][0])
            pages.append(offset)
            page = values[offset:offset + 2]
            return page, {"Content-Range": f"{offset}-{offset + len(page) - 1}/3"}

        client._request = request
        got = client.read_interval(240)
        t.check("full prefix pagination rows", len(got), 3)
        t.check("pagination continued to count", pages, [0, 2])
    finally:
        op.PAGE_SIZE = old_page_size


def test_historic_classification_and_heads(t):
    historic = row(240, "2026-04-04T04:00:00Z", op.CATCHUP_SOURCE)
    manifest = op.build_manifest(
        run_id=3, run_attempt=1, mode="catch-up-apply",
        run_started_at="2026-04-04T08:05:00Z",
        execution_commit_sha=EXECUTION_SHA,
        daily_target_min_ts=op.DAILY_TARGET_MIN_TS,
        predecessor_identity="legacy", predecessor_digest="legacy",
        expected_latest={1440: op.DAILY_TARGET_MIN_TS, 240: historic["ts"]},
        recoverable_minimum={1440: op.DAILY_TARGET_MIN_TS, 240: historic["ts"]},
        intended_source=op.CATCHUP_SOURCE, rows=[historic],
    )
    accepted = op.classify_existing_rows(
        [historic], accepted_keys={op.row_key(historic)}, fetched_rows={},
        historic_manifests=[], source_allowlist=op.CATCHUP_SOURCE_ALLOWLIST,
    )
    t.check("accepted historic row", accepted["accepted_anchor"], 1)
    covered = op.classify_existing_rows(
        [historic], accepted_keys=set(), fetched_rows={},
        historic_manifests=[manifest], source_allowlist=op.CATCHUP_SOURCE_ALLOWLIST,
    )
    t.check("manifest historic match", covered["manifest_verifiable_historic"], 1)
    changed = dict(historic, close="99")
    mismatched_manifest = op.classify_existing_rows(
        [changed], accepted_keys=set(), fetched_rows={},
        historic_manifests=[manifest], source_allowlist=op.CATCHUP_SOURCE_ALLOWLIST,
    )
    t.check("manifest historic mismatch is unverifiable",
            len(mismatched_manifest["unverifiable_historic"]), 1)
    uncovered = op.classify_existing_rows(
        [historic], accepted_keys=set(), fetched_rows={},
        historic_manifests=[], source_allowlist=op.CATCHUP_SOURCE_ALLOWLIST,
    )
    t.check("unanchored historic", len(uncovered["unverifiable_historic"]), 1)

    rows = {1440: [row(1440, op.DAILY_TARGET_MIN_TS, op.CATCHUP_SOURCE)],
            240: [historic]}
    genesis = one_row_bundle(rows)
    descendant_rows = {
        1440: rows[1440] + [
            row(1440, "2024-11-20T00:00:00Z", op.INCREMENTAL_SOURCE)
        ],
        240: rows[240] + [
            row(240, "2026-04-04T08:00:00Z", op.INCREMENTAL_SOURCE)
        ],
    }
    descendant = operational_bundle(
        descendant_rows,
        identity="run:4:attempt:1:operational_head",
        predecessor_identity="run:2:attempt:1:operational_head",
        predecessor_digest="expired-predecessor-digest",
        genesis_identity=genesis.identity,
        run_id=4,
    )
    selected, prefixes = op.select_verified_head(
        FakeDb(descendant_rows), genesis, [descendant]
    )
    t.check("live descendant survives expired predecessor", selected.identity,
            descendant.identity)
    t.check("descendant full prefix verified",
            {key: len(value) for key, value in prefixes.items()},
            {1440: 2, 240: 2})

    invalid_anchors = dict(descendant.anchors)
    daily_anchor = invalid_anchors[1440]
    invalid_anchors[1440] = op.Anchor(
        1440, daily_anchor.min_ts, daily_anchor.accepted_through_ts,
        daily_anchor.count, "0" * 64,
    )
    invalid = op.AnchorBundle(
        descendant.identity, descendant.predecessor_identity,
        descendant.predecessor_digest, descendant.genesis_identity,
        descendant.run_id, descendant.run_attempt, descendant.source_allowlist,
        invalid_anchors,
    )
    t.check("live descendant invalid prefix rejected", error_code(lambda:
        op.select_verified_head(FakeDb(descendant_rows), genesis, [invalid])
    ), "ACCEPTED_ANCHOR_MISMATCH")

    predecessor_digest = op.combined_anchor_digest(genesis.anchors)
    daily_child = operational_bundle(
        {1440: descendant_rows[1440], 240: rows[240]},
        identity="run:5:attempt:1:operational_head",
        predecessor_identity=genesis.identity,
        predecessor_digest=predecessor_digest,
        genesis_identity=genesis.identity,
        run_id=5,
    )
    four_child = operational_bundle(
        {1440: rows[1440], 240: descendant_rows[240]},
        identity="run:6:attempt:1:operational_head",
        predecessor_identity=genesis.identity,
        predecessor_digest=predecessor_digest,
        genesis_identity=genesis.identity,
        run_id=6,
    )
    t.check("two conflicting valid descendants fork", error_code(lambda:
        op.select_verified_head(
            FakeDb(descendant_rows), genesis, [daily_child, four_child]
        )
    ), "ANCHOR_FORK")
    t.check("head advances only through verified", op.advance_verified_through(
        "2026-04-04T04:00:00Z",
        ["2026-04-04T08:00:00Z", "2026-04-04T12:00:00Z"],
        {"2026-04-04T08:00:00Z"}, 240,
    ), "2026-04-04T08:00:00Z")

    head_doc = op.build_anchor_document(
        kind="operational_head", run_id=2, run_attempt=1,
        predecessor=genesis, manifest_identity=None,
        rows_by_interval=rows, genesis_identity=genesis.identity,
    )
    failed_run = {"conclusion": "failure", "head_branch": "main", "run_attempt": 1}
    t.check("failed workflow cannot authorize head", error_code(lambda:
        op.accept_head_candidate(
            head_doc, workflow_run=failed_run, genesis_identity=genesis.identity
        )), "HEAD_UNAVAILABLE")

    artifact_client = object.__new__(op.GitHubArtifactClient)
    artifact_client.list_artifacts = lambda **kwargs: [{
        "id": 88,
        "name": "ohlc-operational-head-2-1",
        "digest": "sha256:" + "a" * 64,
        "expired": False,
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-04-01T00:00:00Z",
    }]
    artifact_client.workflow_run = lambda run_id, attempt: {
        "id": run_id,
        "run_attempt": attempt,
        "head_branch": "main",
        "path": op.WORKFLOW_PATH,
        "conclusion": "failure",
    }
    downloads = []
    artifact_client.download_json = lambda metadata, filename: downloads.append(filename)
    live = artifact_client.load_head_candidates(genesis)
    rebuilt, _ = op.select_verified_head(FakeDb(rows), genesis, live)
    t.check("failed head ignored for rebuild adjudication", rebuilt.identity,
            genesis.identity)
    t.check("failed head document not accepted", downloads, [])


def test_constants_and_confirmation(t):
    t.check("daily target constant", op.validate_daily_target(
        "2024-11-19T00:00:00Z"), op.DAILY_TARGET_MIN_TS)
    later_target = "2025-01-01T00:00:00Z"
    t.check("later owner-approved target", op.validate_daily_target(later_target),
            later_target)
    t.check("missing target", error_code(lambda: op.validate_daily_target(None)),
            "MISSING_DAILY_TARGET")
    t.check("malformed target", error_code(lambda: op.validate_daily_target("bad")),
            "MALFORMED_DAILY_TARGET")
    t.check("off-grid target", error_code(lambda: op.validate_daily_target(
        "2024-11-19T01:00:00Z")), "OFF_GRID_DAILY_TARGET")
    t.check("non-UTC target", error_code(lambda: op.validate_daily_target(
        "2024-11-19T00:00:00-05:00")), "MALFORMED_DAILY_TARGET")
    for forbidden in ("daily_target_min_ts", "genesis_anchor", "source_allowlist"):
        t.check(f"{forbidden} dispatch rejected", error_code(lambda key=forbidden:
            op.validate_dispatch_inputs({"mode": "dry-run", "confirmation": "", key: "x"})
        ), "FORBIDDEN_DISPATCH_INPUT")
    t.check("apply confirmation", op.parse_confirmation(
        "catch-up-apply", "APPLY catch-up").mode, "catch-up-apply")
    t.check("mode/confirmation mismatch", error_code(lambda: op.parse_confirmation(
        "incremental-apply", "APPLY catch-up")), "CONFIRMATION_MISMATCH")
    t.check("malformed resume", error_code(lambda: op.parse_confirmation(
        "incremental-resume",
        "RESUME incremental RUN_ID=1 RUN_ATTEMPT=2 trailing",
    )), "CONFIRMATION_MISMATCH")
    t.check("target not auto-mutated", op.DAILY_TARGET_MIN_TS,
            "2024-11-19T00:00:00Z")

    env = {name: "UNINITIALIZED" for name in (
        "GENESIS_IDENTITY", "GENESIS_ARTIFACT_ID", "GENESIS_ARTIFACT_NAME",
        "GENESIS_ARTIFACT_DIGEST", "GENESIS_RUN_ID", "GENESIS_RUN_ATTEMPT",
        "GENESIS_1440_MIN_TS", "GENESIS_1440_THROUGH_TS", "GENESIS_1440_COUNT",
        "GENESIS_1440_CUMULATIVE_SHA256", "GENESIS_240_MIN_TS",
        "GENESIS_240_THROUGH_TS", "GENESIS_240_COUNT",
        "GENESIS_240_CUMULATIVE_SHA256",
    )}
    t.check("genesis clearly uninitialized", op.parse_genesis_constants(env), None)

    genesis_rows = {
        1440: [row(1440, op.DAILY_TARGET_MIN_TS, op.CATCHUP_SOURCE)],
        240: [row(240, op.FOUR_HOUR_TARGET_MIN_TS, op.CATCHUP_SOURCE)],
    }
    genesis = one_row_bundle(genesis_rows)
    configured = {
        "GENESIS_IDENTITY": genesis.identity,
        "GENESIS_ARTIFACT_ID": "9",
        "GENESIS_ARTIFACT_NAME": "ohlc-candidate-genesis-1-1",
        "GENESIS_ARTIFACT_DIGEST": "a" * 64,
        "GENESIS_RUN_ID": "1",
        "GENESIS_RUN_ATTEMPT": "1",
        "GENESIS_1440_MIN_TS": genesis.anchors[1440].min_ts,
        "GENESIS_1440_THROUGH_TS": genesis.anchors[1440].accepted_through_ts,
        "GENESIS_1440_COUNT": str(genesis.anchors[1440].count),
        "GENESIS_1440_CUMULATIVE_SHA256":
            genesis.anchors[1440].cumulative_digest,
        "GENESIS_240_MIN_TS": genesis.anchors[240].min_ts,
        "GENESIS_240_THROUGH_TS": genesis.anchors[240].accepted_through_ts,
        "GENESIS_240_COUNT": str(genesis.anchors[240].count),
        "GENESIS_240_CUMULATIVE_SHA256":
            genesis.anchors[240].cumulative_digest,
    }
    owner_env = runtime_env(DAILY_TARGET_MIN_TS=later_target, **configured)
    authority = op.validate_runtime_constants(owner_env)
    parsed_genesis = op.parse_genesis_constants(owner_env)
    t.check("advanced target remains runtime authority",
            authority.daily_target_min_ts, later_target)
    t.check("older genesis minimum remains valid",
            parsed_genesis.anchors[1440].min_ts, op.DAILY_TARGET_MIN_TS)


def test_runtime_authority_before_io(t):
    authority = op.validate_runtime_constants(runtime_env())
    t.check("real repository accepted", authority.repository, "monkroe/dca-bot")
    t.check("main accepted", authority.ref_name, "main")
    t.check("execution SHA accepted", authority.execution_commit_sha, EXECUTION_SHA)
    t.check("other repository rejected", error_code(lambda:
        op.validate_runtime_constants(runtime_env(
            GITHUB_REPOSITORY="robert-os-hub/dca-bot"
        ))
    ), "REPOSITORY_MISMATCH")
    t.check("non-main rejected", error_code(lambda:
        op.validate_runtime_constants(runtime_env(GITHUB_REF_NAME="feature"))
    ), "CURRENT_RUN_BRANCH_MISMATCH")
    t.check("missing execution SHA rejected", error_code(lambda:
        op.validate_runtime_constants(runtime_env(GITHUB_SHA=""))
    ), "INVALID_EXECUTION_COMMIT_SHA")
    t.check("malformed execution SHA rejected", error_code(lambda:
        op.validate_runtime_constants(runtime_env(GITHUB_SHA="A" * 40))
    ), "INVALID_EXECUTION_COMMIT_SHA")

    db = FakeDb(baseline())
    kraken = FakeKraken({})
    with tempfile.TemporaryDirectory() as tmp:
        code = error_code(lambda: op.prepare_from_environment(
            runtime_env(GITHUB_REF_NAME="feature"),
            run_started_at="2026-07-17T13:40:00Z",
            db=db,
            kraken=kraken,
            github=None,
            state_dir=Path(tmp),
        ))
    t.check("production prepare rejects branch", code,
            "CURRENT_RUN_BRANCH_MISMATCH")
    t.check("branch rejection before DB read", db.reads, [])
    t.check("branch rejection before Kraken fetch", kraken.calls, [])
    t.check("branch rejection before write", db.inserts, [])


def test_canonical_digest(t):
    base = row(1440, "2026-07-18T00:00:00Z", op.CATCHUP_SOURCE)
    db_numeric = dict(base, open=Decimal("1.000"), high=Decimal("1.2000"),
                      low=Decimal("0.900"), close=Decimal("1.100"),
                      vwap=Decimal("1.0500"), volume=Decimal("10.5"))
    t.check("Kraken strings equal DB numeric", op.market_fields_equal(base, db_numeric), True)
    t.check("trailing zeros", op.normalize_decimal("001.23000"), "1.23")
    t.check("exponent normalization", op.normalize_decimal("1.20E+3"), "1200")
    t.check("negative zero", op.normalize_decimal("-0.000"), "0")
    t.check("timestamp UTC", op.canonical_ts("2026-07-17 19:00:00-05:00"),
            "2026-07-18T00:00:00Z")
    line = op.canonical_json_line(base)
    decoded_pairs = json.loads(line, object_pairs_hook=list)
    t.check("fixed field order", [key for key, _ in decoded_pairs],
            list(op.CANONICAL_FIELDS))
    t.check("LF and final LF", line.endswith(b"\n") and b"\r" not in line, True)
    nullable = dict(base, vwap=None)
    t.check("JSON null", b'"vwap":null' in op.canonical_json_line(nullable), True)
    changed = dict(base, source=op.INCREMENTAL_SOURCE)
    t.check("source changes digest", op.row_digest(base) != op.row_digest(changed), True)
    later = row(1440, "2026-07-19T00:00:00Z", op.CATCHUP_SOURCE)
    t.check("row order canonical", op.rowset_digest([later, base]),
            op.rowset_digest([base, later]))


def test_existing_duplicates_and_races(t):
    existing = row(240, "2026-04-04T04:00:00Z", op.CATCHUP_SOURCE)
    fetched = dict(existing)
    result = op.classify_existing_rows(
        [existing], accepted_keys=set(), fetched_rows={op.row_key(fetched): fetched},
        historic_manifests=[], source_allowlist=op.CATCHUP_SOURCE_ALLOWLIST,
    )
    t.check("matching overlap", result["matching_existing"], 1)
    mismatch = dict(fetched, close="9")
    result = op.classify_existing_rows(
        [existing], accepted_keys=set(), fetched_rows={op.row_key(fetched): mismatch},
        historic_manifests=[], source_allowlist=op.CATCHUP_SOURCE_ALLOWLIST,
    )
    t.check("mismatch overlap", len(result["mismatched_existing"]), 1)
    t.check("fetched duplicate", len(fetch(
        [existing, existing], 240, "2026-04-04T12:05:00Z"
    ).duplicate_keys), 1)
    t.check("DB duplicate", error_code(lambda: op.classify_existing_rows(
        [existing, existing], accepted_keys=set(), fetched_rows={},
        historic_manifests=[], source_allowlist=op.CATCHUP_SOURCE_ALLOWLIST,
    )), "DB_DUPLICATE_KEYS")
    t.check("race identical", op.verify_race(existing, fetched), "race_identical")
    t.check("race mismatch", error_code(lambda: op.verify_race(existing, mismatch)),
            "RACE_MISMATCH")


def test_continuity_and_no_repair(t):
    daily = [row(1440, ts, op.CATCHUP_SOURCE)
             for ts in utc_range("2026-01-01T00:00:00Z", 3, 1440)]
    t.check("daily prefix gap", op.continuity_gaps(
        daily[1:], daily[0]["ts"], daily[-1]["ts"], 1440), [daily[0]["ts"]])
    t.check("daily suffix gap", op.continuity_gaps(
        daily[:-1], daily[0]["ts"], daily[-1]["ts"], 1440), [daily[-1]["ts"]])
    t.check("daily permanent gap", op.gap_classification(
        [daily[0]["ts"]], daily[1]["ts"]), "PERMANENT_GAP")
    legacy = [row(240, ts) for ts in utc_range("2026-04-04T04:00:00Z", 3, 240)]
    recoverable = [row(240, ts, op.CATCHUP_SOURCE)
                   for ts in utc_range("2026-04-04T12:00:00Z", 3, 240)]
    t.check("4h legacy/recoverable overlap", op.continuity_gaps(
        legacy + recoverable, legacy[0]["ts"], recoverable[-1]["ts"], 240), [])
    join_gap = legacy[:2] + recoverable[1:]
    t.check("4h join gap", op.continuity_gaps(
        join_gap, legacy[0]["ts"], recoverable[-1]["ts"], 240),
        ["2026-04-04T12:00:00Z"])
    t.check("incremental permanent gap", op.gap_classification(
        ["2026-04-04T08:00:00Z"], "2026-04-04T12:00:00Z"), "PERMANENT_GAP")
    source = (ROOT / "src" / "ohlc_preserve.py").read_text(encoding="utf-8").lower()
    t.check("no forward-fill implementation", "ffill" in source, False)
    t.check("no other provider", "coingecko" in source, False)


def test_manifest_and_artifact_gates(t):
    intended = row(240, "2026-04-04T08:00:00Z", op.CATCHUP_SOURCE)
    manifest = op.build_manifest(
        run_id=7, run_attempt=2, mode="catch-up-apply",
        run_started_at="2026-04-04T12:05:00Z",
        execution_commit_sha=EXECUTION_SHA,
        daily_target_min_ts=op.DAILY_TARGET_MIN_TS,
        predecessor_identity="legacy", predecessor_digest="digest",
        expected_latest={1440: op.DAILY_TARGET_MIN_TS, 240: intended["ts"]},
        recoverable_minimum={1440: op.DAILY_TARGET_MIN_TS, 240: intended["ts"]},
        intended_source=op.CATCHUP_SOURCE, rows=[intended],
    )
    op.validate_manifest(manifest, run_id=7, run_attempt=2)
    t.check("manifest valid", True, True)
    t.check("manifest repository authority", manifest["repo"], "monkroe/dca-bot")
    t.check("manifest frozen baseline SHA", manifest["baseline_commit_sha"],
            op.BASELINE_COMMIT_SHA)
    t.check("manifest execution commit SHA", manifest["execution_commit_sha"],
            EXECUTION_SHA)
    t.check("run ID mismatch", error_code(lambda: op.validate_manifest(
        manifest, run_id=8)), "RUN_ID_MISMATCH")
    t.check("attempt mismatch", error_code(lambda: op.validate_manifest(
        manifest, run_attempt=3)), "RUN_ATTEMPT_MISMATCH")
    damaged = dict(manifest)
    damaged["intended_source"] = op.INCREMENTAL_SOURCE
    t.check("manifest digest mismatch", error_code(lambda:
        op.validate_manifest(damaged)), "MANIFEST_DIGEST_MISMATCH")
    t.check("retention 90 accepted", op.artifact_retention_ok(
        "2026-01-01T00:00:00Z", "2026-04-01T00:00:00Z"), True)
    t.check("short retention blocks", op.artifact_retention_ok(
        "2026-01-01T00:00:00Z", "2026-03-31T23:59:59Z"), False)
    t.check("short retention code", error_code(lambda: op.verify_artifact_metadata(
        {"id": 1, "name": "x", "digest": "sha256:" + "a" * 64,
         "expired": False, "created_at": "2026-01-01T00:00:00Z",
         "expires_at": "2026-03-31T23:59:59Z"},
        artifact_id=1, name="x", platform_digest="a" * 64,
    )), "ARTIFACT_RETENTION_TOO_SHORT")
    t.check("platform digest mismatch", error_code(lambda:
        op.verify_artifact_metadata(
            {"id": 1, "name": "x", "digest": "sha256:" + "b" * 64,
             "expired": False, "created_at": "2026-01-01T00:00:00Z",
             "expires_at": "2026-04-01T00:00:00Z"},
            artifact_id=1, name="x", platform_digest="a" * 64,
        )), "ARTIFACT_DIGEST_MISMATCH")
    t.check("missing service-role principal", error_code(lambda:
        op.SupabaseClient("https://example.invalid", "")),
        "MISSING_SERVICE_ROLE_PRINCIPAL")

    artifact_client = object.__new__(op.GitHubArtifactClient)
    artifact_client.list_artifacts = lambda **kwargs: [{
        "id": 70,
        "name": "ohlc-prewrite-7-2-catch-up-apply",
        "digest": "sha256:" + "a" * 64,
        "expired": False,
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-04-01T00:00:00Z",
    }]
    artifact_client.workflow_run = lambda run_id, attempt: {
        "id": run_id,
        "run_attempt": attempt,
        "head_branch": "main",
        "head_sha": OTHER_EXECUTION_SHA,
        "path": op.WORKFLOW_PATH,
        "conclusion": "failure",
    }
    artifact_client.download_json = lambda metadata, filename: manifest
    t.check("cross-run workflow head SHA mismatch", error_code(lambda:
        artifact_client.load_resume_manifest(7, 2, "catch-up")
    ), "EXECUTION_PROVENANCE_MISMATCH")


def test_repeated_partial_resume_lineage(t):
    values = baseline()
    legacy = op.legacy_bundle(values)
    additions = [
        row(1440, ts, op.CATCHUP_SOURCE)
        for ts in (
            "2026-07-17T00:00:00Z",
            "2026-07-18T00:00:00Z",
            "2026-07-19T00:00:00Z",
        )
    ]
    manifest_one = op.build_manifest(
        run_id=10,
        run_attempt=1,
        mode="catch-up-apply",
        run_started_at="2026-07-20T12:05:00Z",
        execution_commit_sha=EXECUTION_SHA,
        daily_target_min_ts=op.DAILY_TARGET_MIN_TS,
        predecessor_identity=legacy.identity,
        predecessor_digest=op.combined_anchor_digest(legacy.anchors),
        expected_latest={1440: additions[-1]["ts"],
                         240: values[240][-1]["ts"]},
        recoverable_minimum={1440: additions[-1]["ts"],
                             240: values[240][-1]["ts"]},
        intended_source=op.CATCHUP_SOURCE,
        rows=additions,
    )
    artifact_one = {
        "id": 101,
        "name": "ohlc-prewrite-10-1-catch-up-apply",
        "digest": "sha256:" + "a" * 64,
    }

    db = FakeDb(values)
    db.insert_batch([additions[0]])
    op.validate_state_b(db.rows, manifest_one, "catch-up")
    manifest_two = op.build_manifest(
        run_id=11,
        run_attempt=1,
        mode="catch-up-resume",
        run_started_at="2026-07-20T12:05:00Z",
        execution_commit_sha=EXECUTION_SHA,
        daily_target_min_ts=op.DAILY_TARGET_MIN_TS,
        predecessor_identity=legacy.identity,
        predecessor_digest=op.combined_anchor_digest(legacy.anchors),
        expected_latest={1440: additions[-1]["ts"],
                         240: values[240][-1]["ts"]},
        recoverable_minimum={1440: additions[-1]["ts"],
                             240: values[240][-1]["ts"]},
        intended_source=op.CATCHUP_SOURCE,
        rows=additions[1:],
        predecessor_evidence=[(manifest_one, artifact_one)],
    )
    db.insert_batch([additions[1]])
    op.validate_state_b(db.rows, manifest_two, "catch-up")
    artifact_two = {
        "id": 102,
        "name": "ohlc-prewrite-11-1-catch-up-resume",
        "digest": "sha256:" + "b" * 64,
    }
    manifest_three = op.build_manifest(
        run_id=12,
        run_attempt=1,
        mode="catch-up-resume",
        run_started_at="2026-07-20T12:05:00Z",
        execution_commit_sha=EXECUTION_SHA,
        daily_target_min_ts=op.DAILY_TARGET_MIN_TS,
        predecessor_identity=legacy.identity,
        predecessor_digest=op.combined_anchor_digest(legacy.anchors),
        expected_latest={1440: additions[-1]["ts"],
                         240: values[240][-1]["ts"]},
        recoverable_minimum={1440: additions[-1]["ts"],
                             240: values[240][-1]["ts"]},
        intended_source=op.CATCHUP_SOURCE,
        rows=additions[2:],
        predecessor_evidence=[(manifest_two, artifact_two)],
    )
    op.validate_state_b(db.rows, manifest_three, "catch-up")
    covered = op.classify_existing_rows(
        additions[:2],
        accepted_keys=set(),
        fetched_rows={},
        historic_manifests=[manifest_three],
        source_allowlist=op.CATCHUP_SOURCE_ALLOWLIST,
    )
    t.check("second resume verifies both earlier subsets",
            covered["manifest_verifiable_historic"], 2)
    t.check("latest resume retains complete lineage",
            [(item["run_id"], item["run_attempt"])
             for item in manifest_three["authorized_lineage"]],
            [(10, 1), (11, 1)])

    before_second_resume = list(db.inserted_keys)
    op.insert_with_race_readback(db, 1440, [additions[2]])
    t.check("second resume writes only remaining row",
            db.inserted_keys[len(before_second_resume):],
            [op.row_key(additions[2])])
    t.check("final continuity passes", op.continuity_gaps(
        db.rows[1440],
        values[1440][0]["ts"],
        additions[-1]["ts"],
        1440,
    ), [])

    foreign = row(1440, "2026-07-20T00:00:00Z", op.CATCHUP_SOURCE)
    foreign_rows = {
        1440: db.rows[1440] + [foreign],
        240: db.rows[240],
    }
    t.check("foreign row outside lineage remains unverifiable",
            error_code(lambda: op.validate_state_b(
                foreign_rows, manifest_three, "catch-up"
            )), "UNVERIFIABLE_HISTORIC")


def make_apply_state(directory, *, daily_rows, four_rows, daily_new=None,
                     four_new=None, mode="incremental-apply",
                     daily_target=op.DAILY_TARGET_MIN_TS):
    run_id, attempt = 77, 2
    intended = [item for item in (daily_new, four_new) if item is not None]
    accepted = one_row_bundle({1440: daily_rows, 240: four_rows})
    expected = {
        1440: daily_new["ts"] if daily_new else daily_rows[-1]["ts"],
        240: four_new["ts"] if four_new else four_rows[-1]["ts"],
    }
    manifest = op.build_manifest(
        run_id=run_id, run_attempt=attempt, mode=mode,
        run_started_at="2026-04-05T12:05:00Z",
        execution_commit_sha=EXECUTION_SHA,
        daily_target_min_ts=daily_target,
        predecessor_identity=accepted.identity,
        predecessor_digest=op.combined_anchor_digest(accepted.anchors),
        expected_latest=expected,
        recoverable_minimum={1440: daily_rows[0]["ts"], 240: four_rows[0]["ts"]},
        intended_source=op.INCREMENTAL_SOURCE, rows=intended,
    )
    manifest_path = directory / f"prewrite-{run_id}-{attempt}.json"
    manifest_path.write_bytes(op.canonical_document(manifest))
    plans = {}
    for interval, current, new in (
        (1440, daily_rows, daily_new), (240, four_rows, four_new)
    ):
        plans[str(interval)] = {
            "would_insert_rows": [op.normalize_row(new)] if new else [],
            "expected_latest_committed_ts": expected[interval],
            "recoverable_minimum": current[0]["ts"],
            "daily_target_min_ts": daily_target,
        }
    state = op.seal_document({
        "schema_version": "ohlc-preservation-state-v1.3",
        "run_id": run_id,
        "run_attempt": attempt,
        "mode": mode,
        "run_started_at": "2026-04-05T12:05:00Z",
        "execution_commit_sha": EXECUTION_SHA,
        "daily_target_min_ts": daily_target,
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_path": str(manifest_path),
        "accepted": op.bundle_to_dict(accepted),
        "genesis_identity": accepted.identity,
        "intended_source": op.INCREMENTAL_SOURCE,
        "source_allowlist": list(op.INCREMENTAL_SOURCE_ALLOWLIST),
        "db_snapshot": {
            "1440": [op.normalize_row(item) for item in daily_rows],
            "240": [op.normalize_row(item) for item in four_rows],
        },
        "plans": plans,
    }, "state_sha256")
    state_path = directory / f"state-{run_id}-{attempt}.json"
    state_path.write_bytes(op.canonical_document(state))
    return state_path, f"ohlc-prewrite-{run_id}-{attempt}-{mode}"


def test_apply_order_and_failures(t):
    daily = [row(1440, op.DAILY_TARGET_MIN_TS, op.CATCHUP_SOURCE)]
    four = [row(240, op.FOUR_HOUR_TARGET_MIN_TS, op.CATCHUP_SOURCE)]
    daily_new = row(1440, "2024-11-20T00:00:00Z", op.INCREMENTAL_SOURCE)
    four_new = row(240, "2026-04-04T08:00:00Z", op.INCREMENTAL_SOURCE)
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        state_path, artifact_name = make_apply_state(
            directory, daily_rows=daily, four_rows=four,
            daily_new=daily_new, four_new=four_new,
        )
        github = FakeGithub()
        github.artifact_name = artifact_name
        state = op._read_document(state_path)
        github.uploaded_manifest = op._read_document(Path(state["manifest_path"]))
        db = FakeDb({1440: daily, 240: four})
        result = op.apply_run(
            state_path=state_path, artifact_id=9, artifact_name=artifact_name,
            platform_digest="a" * 64, db=db, github=github, state_dir=directory,
            execution_commit_sha=EXECUTION_SHA,
        )
        t.check("daily write precedes 4h", db.inserts, [1440, 240])
        t.check("post-insert rereads happened", len(db.reads) >= 6, True)
        t.check("operational head produced", result["anchor"]["kind"],
                "operational_head")

        daily_fail = FakeDb({1440: daily, 240: four}, fail_interval=1440)
        t.check("partial daily failure", error_code(lambda: op.apply_run(
            state_path=state_path, artifact_id=9, artifact_name=artifact_name,
            platform_digest="a" * 64, db=daily_fail, github=github,
            state_dir=directory,
            execution_commit_sha=EXECUTION_SHA,
        )), "TEST_INSERT_FAILURE")
        t.check("daily failure blocks 4h", daily_fail.inserts, [1440])

        verify_fail = FakeDb(
            {1440: daily, 240: four}, drop_insert_interval=1440
        )
        t.check("daily verification failure", error_code(lambda: op.apply_run(
            state_path=state_path, artifact_id=9, artifact_name=artifact_name,
            platform_digest="a" * 64, db=verify_fail, github=github,
            state_dir=directory,
            execution_commit_sha=EXECUTION_SHA,
        )), "POST_WRITE_REREAD_MISSING")
        t.check("verification failure blocks 4h", verify_fail.inserts, [1440])

        four_fail = FakeDb({1440: daily, 240: four}, fail_interval=240)
        t.check("partial 4h failure", error_code(lambda: op.apply_run(
            state_path=state_path, artifact_id=9, artifact_name=artifact_name,
            platform_digest="a" * 64, db=four_fail, github=github,
            state_dir=directory,
            execution_commit_sha=EXECUTION_SHA,
        )), "TEST_INSERT_FAILURE")
        t.check("4h attempted only after daily", four_fail.inserts, [1440, 240])

        state_path_empty, artifact_empty = make_apply_state(
            directory, daily_rows=daily, four_rows=four,
            daily_new=None, four_new=None,
        )
        github.artifact_name = artifact_empty
        empty_state = op._read_document(state_path_empty)
        github.uploaded_manifest = op._read_document(
            Path(empty_state["manifest_path"])
        )
        idempotent = FakeDb({1440: daily, 240: four})
        op.apply_run(
            state_path=state_path_empty, artifact_id=9,
            artifact_name=artifact_empty, platform_digest="a" * 64,
            db=idempotent, github=github, state_dir=directory,
            execution_commit_sha=EXECUTION_SHA,
        )
        t.check("idempotent apply has no inserts", idempotent.inserts, [])

        later_target = "2024-11-20T00:00:00Z"
        daily_with_old_genesis = [
            row(1440, op.DAILY_TARGET_MIN_TS, op.CATCHUP_SOURCE),
            row(1440, later_target, op.CATCHUP_SOURCE),
        ]
        daily_after_target = row(
            1440, "2024-11-21T00:00:00Z", op.INCREMENTAL_SOURCE
        )
        changed_state, changed_artifact = make_apply_state(
            directory,
            daily_rows=daily_with_old_genesis,
            four_rows=four,
            daily_new=daily_after_target,
            daily_target=later_target,
        )
        github.artifact_name = changed_artifact
        changed_document = op._read_document(changed_state)
        github.uploaded_manifest = op._read_document(
            Path(changed_document["manifest_path"])
        )
        changed_db = FakeDb({1440: daily_with_old_genesis, 240: four})
        changed_result = op.apply_run(
            state_path=changed_state,
            artifact_id=9,
            artifact_name=changed_artifact,
            platform_digest="a" * 64,
            db=changed_db,
            github=github,
            state_dir=directory,
            execution_commit_sha=EXECUTION_SHA,
        )
        t.check("apply uses sealed advanced daily target", changed_db.inserts,
                [1440])
        t.check("head retains older genesis minimum",
                changed_result["anchor"]["anchors"]["1440"]["min_ts"],
                op.DAILY_TARGET_MIN_TS)


def test_dry_run_and_rebuild_failure(t):
    values = baseline()
    started = "2026-07-17T13:40:00Z"
    daily_all = [
        row(1440, ts, op.CATCHUP_SOURCE)
        for ts in op.timestamps_between(
            op.DAILY_TARGET_MIN_TS,
            op.expected_latest_committed_ts(started, 1440), 1440
        )
    ]
    four_all = [
        row(240, ts, op.CATCHUP_SOURCE)
        for ts in op.timestamps_between(
            op.FOUR_HOUR_TARGET_MIN_TS,
            op.expected_latest_committed_ts(started, 240), 240
        )
    ]
    db = FakeDb(values)
    kraken = FakeKraken({1440: kraken_payload(daily_all),
                         240: kraken_payload(four_all)})
    with tempfile.TemporaryDirectory() as tmp:
        with redirect_stdout(io.StringIO()):
            result = op.prepare_run(
                mode="dry-run", confirmation_text="", run_started_at=started,
                run_id=1, run_attempt=1, db=db, kraken=kraken,
                github=None, genesis=None, state_dir=Path(tmp),
                execution_commit_sha=EXECUTION_SHA,
                daily_target_min_ts=op.DAILY_TARGET_MIN_TS,
            )
    t.check("dry-run has no write", db.inserts, [])
    t.check("dry-run reports both intervals", sorted(result["report"]),
            ["1440", "240"])

    old_daily = row(1440, op.DAILY_TARGET_MIN_TS, op.CATCHUP_SOURCE)
    advanced_target = "2024-11-20T00:00:00Z"
    advanced_rows = [
        row(1440, advanced_target, op.INCREMENTAL_SOURCE),
        row(1440, "2024-11-21T00:00:00Z", op.INCREMENTAL_SOURCE),
    ]
    advanced_anchor = op.Anchor(
        1440, old_daily["ts"], old_daily["ts"], 1,
        op.rowset_digest([old_daily]),
    )
    advanced_plan = op.plan_interval(
        interval=1440,
        run_started_at="2024-11-22T12:05:00Z",
        db_rows=[old_daily],
        accepted_prefix=[old_daily],
        accepted_anchor=advanced_anchor,
        fetch=fetch(
            advanced_rows, 1440, "2024-11-22T12:05:00Z",
            op.INCREMENTAL_SOURCE,
        ),
        lifecycle_state="C",
        intended_source=op.INCREMENTAL_SOURCE,
        source_allowlist=op.INCREMENTAL_SOURCE_ALLOWLIST,
        daily_target_min_ts=advanced_target,
        historic_manifests=[],
    )
    t.check("planning uses advanced daily target",
            advanced_plan["daily_target_min_ts"], advanced_target)
    t.check("planning inserts from advanced target",
            advanced_plan["would_insert"], 2)

    historic = row(240, "2026-04-04T08:00:00Z", op.INCREMENTAL_SOURCE)
    classified = op.classify_existing_rows(
        [historic], accepted_keys=set(), fetched_rows={},
        historic_manifests=[], source_allowlist=op.INCREMENTAL_SOURCE_ALLOWLIST,
    )
    t.check("rebuild fails on unverifiable row",
            bool(classified["unverifiable_historic"]), True)

    accepted = row(240, op.FOUR_HOUR_TARGET_MIN_TS, op.CATCHUP_SOURCE)
    next_db = row(240, "2026-04-04T08:00:00Z", op.INCREMENTAL_SOURCE)
    future = row(240, "2026-04-04T12:00:00Z", op.INCREMENTAL_SOURCE)
    anchor = op.Anchor(240, accepted["ts"], accepted["ts"], 1,
                       op.rowset_digest([accepted]))
    rebuild_plan = op.plan_interval(
        interval=240,
        run_started_at="2026-04-04T16:05:00Z",
        db_rows=[accepted, next_db],
        accepted_prefix=[accepted],
        accepted_anchor=anchor,
        fetch=fetch([accepted, next_db, future], 240, "2026-04-04T16:05:00Z",
                    op.INCREMENTAL_SOURCE),
        lifecycle_state="C",
        intended_source=op.INCREMENTAL_SOURCE,
        source_allowlist=op.INCREMENTAL_SOURCE_ALLOWLIST,
        daily_target_min_ts=op.DAILY_TARGET_MIN_TS,
        historic_manifests=[],
        read_only_rebuild=True,
    )
    t.check("read-only rebuild proposes no DB insert",
            rebuild_plan["would_insert"], 0)
    t.check("read-only rebuild stops at DB maximum",
            rebuild_plan["prospective_max"], next_db["ts"])


def test_isolation_and_workflow(t):
    workflow = (ROOT / ".github" / "workflows" /
                "kraken_ohlc_preserve.yml").read_text(encoding="utf-8")
    t.check("workflow dispatch only", "workflow_dispatch:" in workflow
            and "\n  schedule:" not in workflow, True)
    t.check("read permissions exact", "actions: read" in workflow
            and "contents: read" in workflow, True)
    for forbidden in (
        "contents: write", "actions: write", "id-token: write",
        "packages: write", "issues: write", "pull-requests: write",
        "deployments: write", "KRAKEN_API_KEY", "KRAKEN_API_SECRET",
        "KRAKEN_RO_API_KEY", "KRAKEN_RO_API_SECRET", "TG_BOT_TOKEN",
        "TG_CHAT_ID",
    ):
        t.check(f"workflow excludes {forbidden}", forbidden in workflow, False)
    t.check("artifact overwrite forbidden", "overwrite: false" in workflow, True)
    t.check("retention 90", workflow.count("retention-days: 90") >= 2, True)
    t.check("concurrency isolated", "group: kraken-ohlc-preserve" in workflow, True)
    manifest_upload = workflow.index("- name: Upload pre-write manifest")
    manifest_verify = workflow.index("- name: Verify pre-write artifact metadata")
    apply_step = workflow.index("- name: Apply daily then four-hour rows")
    t.check("manifest upload precedes insert", manifest_upload < apply_step, True)
    t.check("artifact verification precedes insert", manifest_verify < apply_step, True)
    t.check("upload/verification failure blocks apply",
            "always()" not in workflow[manifest_upload:apply_step], True)

    expected = {
        "test.sh": "946ac9b10d7217a1b546707afc88dab75a1f36dd5ca1b81479d8d10bd7f6a116",
        "src/ohlc.py": "ef8d1c07c75aeadb34f6d204188f44bce5887e488015a60ee42df6449f7389a1",
        "src/kraken_run.py": "fc0b1f3019aa07f573b5307a0f40bb731aa2e886f28e64e75b3c1e3bf435d58b",
        ".github/workflows/kraken_dca.yml": "06f95eb58644178ccf5a400c66e360ca1ed770a245ba2f6c975a8c11add8cd17",
        ".github/workflows/kraken_sync.yml": "c7d858e0b7f65c905b4fb524238545ac45477cab0e8b22f2363e56e3fdbd965e",
    }
    for path, digest in expected.items():
        actual = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        t.check(f"{path} unchanged", actual, digest)


if __name__ == "__main__":
    sys.exit(Runner("OHLC preservation contract v1.3").run([
        ("boundary and commitment", test_boundary_and_commitment),
        ("current placement and malformed rows", test_current_not_last_and_bad_rows),
        ("baseline fingerprint", test_baseline_fingerprint),
        ("lifecycle states", test_lifecycle_states),
        ("anchor prefix and pagination", test_anchor_prefix_and_pagination),
        ("historic classification and heads", test_historic_classification_and_heads),
        ("constants and confirmation", test_constants_and_confirmation),
        ("runtime authority before I/O", test_runtime_authority_before_io),
        ("canonical digest", test_canonical_digest),
        ("existing, duplicates, and races", test_existing_duplicates_and_races),
        ("continuity and no repair", test_continuity_and_no_repair),
        ("manifest and artifact gates", test_manifest_and_artifact_gates),
        ("repeated partial resume lineage", test_repeated_partial_resume_lineage),
        ("apply order and failures", test_apply_order_and_failures),
        ("dry-run and rebuild failure", test_dry_run_and_rebuild_failure),
        ("isolation and workflow", test_isolation_and_workflow),
    ]))
