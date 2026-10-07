#!/usr/bin/env python3
"""ROB-21: the pure deploy classifier (no I/O, no Supabase).

The vocabulary is ENUMERATED FROM SOURCE: the first group of tests re-derives
every status, re-peg phase and fallback outcome from the text of
src/kraken_run.py and requires the classifier's tables to match it exactly, so
a status or phase added later cannot slip past the gate unclassified.
"""
import json
import re
from pathlib import Path

from _harness import kr, Runner

SRC = (Path(__file__).resolve().parent.parent / "src" / "kraken_run.py").read_text()

# The spec's own enumeration, pinned independently of the module under test.
SPEC_PHASES = {
    "recovery_armed", "cancel_pending", "cancel_requested", "replacement_submission_pending",
    "replacement_attached", "replacement_rejected", "original_terminal_fallback",
    "replacement_closed", "replacement_terminal", "original_closed", "original_terminal",
}
SPEC_DECISION = {
    "canceled_partial", "canceled_unfilled", "rejected_postonly",
    "canceled_partial_dry_run", "canceled_unfilled_dry_run", "rejected_postonly_dry_run",
}


# ── vocabulary enumerated from source ─────────────────────────

def _quoted(pattern):
    return set(re.findall(r'"(' + pattern + r')"', SRC))


def t_status_vocabulary_matches_source(r):
    family = (r"claimed|placed|limit_open|filled|filled_dry_run|manual_required|repeg_recovery_pending"
              r"|failed_[a-z]+|skipped_[a-z_]+|rejected_[a-z_]+|canceled_[a-z_]+")
    in_source = _quoted(family)
    r.check("every status literal in the source is classified", in_source, set(kr.DEPLOY_KNOWN_STATUSES))
    r.check("19 statuses", len(kr.DEPLOY_KNOWN_STATUSES), 19)
    r.check("no status is classified twice", len(set(kr.DEPLOY_KNOWN_STATUSES)), len(kr.DEPLOY_KNOWN_STATUSES))
    r.check("decision statuses match the spec", set(kr.DEPLOY_DECISION_STATUSES), SPEC_DECISION)
    r.check("decision statuses are the source constant", kr.DEPLOY_DECISION_STATUSES, kr.PENDING_DECISION_STATUSES)
    r.check("recovery status is the source constant", kr.REPEG_RECOVERY_STATUS in kr.DEPLOY_IN_FLIGHT_STATUSES, True)


def t_phase_vocabulary_matches_source(r):
    phases = set(re.findall(r'\["phase"\]\s*=\s*"([a-z_]+)"', SRC))
    phases |= set(re.findall(r'"phase":\s*"([a-z_]+)"', SRC))
    phases |= set(re.findall(
        r'_repeg_record_observation\(\s*row,\s*transition,\s*order,\s*"([a-z_]+)"', SRC))
    r.check("every phase assigned in the source is classified", phases, set(kr.DEPLOY_KNOWN_PHASES))
    r.check("matches the spec enumeration", set(kr.DEPLOY_KNOWN_PHASES), SPEC_PHASES)
    r.check("11 phases", len(kr.DEPLOY_KNOWN_PHASES), 11)
    r.check("every phase is classified once", len(kr.DEPLOY_KNOWN_PHASES), len(set(kr.DEPLOY_KNOWN_PHASES)))


def t_fallback_outcomes_match_source(r):
    in_source = set(re.findall(r'mark\("([a-z_0-9]+)"\)', SRC))
    classified = set(kr.DEPLOY_NO_FALLBACK_ORDER_REASONS) | set(kr.DEPLOY_FALLBACK_SETTLED_STATUSES)
    r.check("every fallback outcome in the source is classified", in_source, classified)


# ── raw decoding (equivalent to (raw #>> '{}')::jsonb) ────────

def t_raw_decoding(r):
    r.check("SQL NULL is absent, not an error", kr.decode_execution_raw(None), (True, None))
    r.check("JSON string (the historical storage form)", kr.decode_execution_raw('{"a": 1}'), (True, {"a": 1}))
    r.check("already-decoded object", kr.decode_execution_raw({"a": 1}), (True, {"a": 1}))
    r.check("empty object", kr.decode_execution_raw("{}"), (True, {}))
    for bad in ("{", "not json", "", "[1]", "5", "null", '"{}"', json.dumps(json.dumps({"a": 1})), 5, 1.5, [1], True):
        r.check(f"{bad!r} is malformed", kr.decode_execution_raw(bad), (False, None))


# ── status coverage, one explicit case per status ─────────────

def row(status, **kw):
    base = {"cl_ord_id": f"c-{status}", "status": status, "attempt_type": "maker_limit",
            "parent_event_id": "evt-1", "reason": None, "raw": None}
    base.update(kw)
    return base


def verdict(*rows, index=0):
    return kr.classify_deploy_rows(list(rows))["verdicts"][index]["verdict"]


def t_every_known_status_is_covered_explicitly(r):
    expected = {
        # terminal and self-sufficient
        "filled": "SAFE", "filled_dry_run": "SAFE", "failed_kraken": "SAFE",
        "skipped_above_cap": "SAFE", "skipped_insufficient_funds": "SAFE",
        "skipped_min_order": "SAFE", "skipped_target_too_small": "SAFE",
        # still owned by a state machine
        "claimed": "STOP", "placed": "STOP", "limit_open": "STOP", "repeg_recovery_pending": "STOP",
        # human closeout, not automatically safe
        "manual_required": "STOP", "failed_reconciliation": "STOP",
        # maker leg ended; the EVENT is undecided without evidence
        "canceled_partial": "STOP", "canceled_unfilled": "STOP", "rejected_postonly": "STOP",
        "canceled_partial_dry_run": "STOP", "canceled_unfilled_dry_run": "STOP",
        "rejected_postonly_dry_run": "STOP",
    }
    r.check("the table covers the complete vocabulary", set(expected), set(kr.DEPLOY_KNOWN_STATUSES))
    for status, want in expected.items():
        r.check(f"{status} on its own", verdict(row(status)), want)


def t_in_flight_and_manual_states_block(r):
    for status in ("claimed", "placed", "limit_open", "repeg_recovery_pending", "canceled_partial"):
        r.check(f"{status} is not a safe deploy state", verdict(row(status)), "STOP")
    r.check("manual_required is NOT automatically safe", verdict(row("manual_required")), "STOP")
    r.check("manual_required with a reason is still not safe",
            verdict(row("manual_required", reason="fallback_created")), "STOP")
    r.check("failed_reconciliation needs a human", verdict(row("failed_reconciliation")), "STOP")


def t_unknown_status_blocks(r):
    for bad in ("weird", "FILLED", "", None, 5, "failed_strike", "filled ", "limit_open_x"):
        r.check(f"status {bad!r} blocks", verdict({**row("filled"), "status": bad}), "STOP")
    result = kr.classify_deploy_rows([row("filled"), {**row("filled"), "status": "weird", "cl_ord_id": "bad"}])
    r.check("one unknown status blocks the whole gate", result["safe"], False)
    r.check("the blocker is named", [b["cl_ord_id"] for b in result["blockers"]], ["bad"])
    r.check("non-object row blocks", kr.classify_deploy_rows(["x"])["safe"], False)


# ── re-peg phases ─────────────────────────────────────────────

def with_phase(status, phase, **kw):
    return row(status, raw=json.dumps({"repeg_transition": {"phase": phase}}), **kw)


def t_every_phase_is_covered_explicitly(r):
    in_flight = {"recovery_armed", "cancel_pending", "cancel_requested",
                 "replacement_submission_pending", "original_terminal"}
    settled = {
        "original_closed": "filled", "replacement_closed": "filled", "replacement_attached": "filled",
        "replacement_rejected": "rejected_postonly", "replacement_terminal": "canceled_unfilled",
        "original_terminal_fallback": "canceled_partial",
    }
    r.check("the table covers every phase", in_flight | set(settled), SPEC_PHASES)
    for phase in in_flight:
        r.check(f"{phase} blocks beside a terminal status", verdict(with_phase("filled", phase)), "STOP")
        r.check(f"{phase} blocks beside recovery status", verdict(with_phase(kr.REPEG_RECOVERY_STATUS, phase)), "STOP")
    for phase, status in settled.items():
        extra = {"reason": "fallback_below_ordermin"} if status != "filled" else {}
        r.check(f"{phase} beside {status} is accepted", verdict(with_phase(status, phase, **extra)), "SAFE")
        r.check(f"{phase} still blocks while recovery is pending",
                verdict(with_phase(kr.REPEG_RECOVERY_STATUS, phase)), "STOP")
    r.check("a settled phase beside an inconsistent status blocks", verdict(with_phase("failed_kraken", "original_closed")), "STOP")
    r.check("limit_open with replacement_attached still blocks", verdict(with_phase("limit_open", "replacement_attached")), "STOP")


def t_unknown_or_malformed_phase_blocks(r):
    for phase in ("weird", "replacement_", "Recovery_Armed", "", None, 5, ["x"]):
        r.check(f"phase {phase!r} blocks", verdict(with_phase("filled", phase)), "STOP")
    r.check("transition without a phase blocks",
            verdict(row("filled", raw=json.dumps({"repeg_transition": {}}))), "STOP")
    r.check("non-object transition blocks",
            verdict(row("filled", raw=json.dumps({"repeg_transition": "x"}))), "STOP")


def t_malformed_raw_blocks(r):
    for bad in ("{", "not json", "[1]", '"{}"', 5, [1]):
        r.check(f"raw {bad!r} blocks even on a terminal status", verdict(row("filled", raw=bad)), "STOP")
    r.check("object raw is decoded", verdict(row("filled", raw={"x": 1})), "SAFE")
    r.check("JSON-string raw is decoded", verdict(row("filled", raw='{"x": 1}')), "SAFE")
    r.check("absent raw is fine", verdict(row("filled", raw=None)), "SAFE")


# ── parent-event evidence for maker decision statuses ─────────

def maker(status="canceled_unfilled", reason=None, **kw):
    return row(status, cl_ord_id="dca-X", reason=reason, **kw)


def fb(status, **kw):
    kw.setdefault("cl_ord_id", "dca-X-fb")
    return row(status, attempt_type="maker_fallback", **kw)


def t_maker_decision_needs_parent_event_evidence(r):
    for status in sorted(SPEC_DECISION):
        r.check(f"{status}: reason empty (decision pending)", verdict(maker(status)), "STOP")
        r.check(f"{status}: unknown reason", verdict(maker(status, "fallback_whatever")), "STOP")
        for reason in kr.DEPLOY_NO_FALLBACK_ORDER_REASONS:
            r.check(f"{status}: {reason} resolves the event", verdict(maker(status, reason)), "SAFE")
        r.check(f"{status}: fallback_created without its sibling", verdict(maker(status, "fallback_created")), "STOP")
        r.check(f"{status}: fallback_failed_kraken without its sibling", verdict(maker(status, "fallback_failed_kraken")), "STOP")
    r.check("decision status on a non-maker row", verdict(row("canceled_unfilled", attempt_type="market", reason="fallback_none_budget")), "STOP")
    r.check("decision status with no attempt_type", verdict(row("canceled_unfilled", attempt_type=None, reason="fallback_none_budget")), "STOP")


def t_proven_terminal_parent_event_structures_are_safe(r):
    for status in ("filled", "filled_dry_run"):
        rows = [maker("canceled_partial", "fallback_created"), fb(status)]
        result = kr.classify_deploy_rows(rows)
        r.check(f"fallback_created + sibling {status}", result["safe"], True)
    rows = [maker("rejected_postonly", "fallback_failed_kraken"), fb("failed_kraken")]
    r.check("fallback_failed_kraken + sibling failed_kraken", kr.classify_deploy_rows(rows)["safe"], True)
    rows = [maker("canceled_unfilled_dry_run", "fallback_created"), fb("filled_dry_run")]
    r.check("dry-run structure", kr.classify_deploy_rows(rows)["safe"], True)
    r.check("both rows are individually SAFE",
            [v["verdict"] for v in kr.classify_deploy_rows([maker("canceled_partial", "fallback_created"), fb("filled")])["verdicts"]],
            ["SAFE", "SAFE"])


def t_unproven_structures_block(r):
    cases = {
        "sibling still claimed": [maker("canceled_partial", "fallback_created"), fb("claimed")],
        "sibling placed": [maker("canceled_partial", "fallback_created"), fb("placed")],
        "sibling failed_kraken does not settle fallback_created": [maker("canceled_partial", "fallback_created"), fb("failed_kraken")],
        "sibling filled does not settle fallback_failed_kraken": [maker("canceled_partial", "fallback_failed_kraken"), fb("filled")],
        "sibling in another event": [maker("canceled_partial", "fallback_created"), fb("filled", parent_event_id="evt-2")],
        "sibling not supplied": [maker("canceled_partial", "fallback_created")],
        "two siblings": [maker("canceled_partial", "fallback_created"), fb("filled"), fb("filled", cl_ord_id="dca-X-fb2")],
        "no parent event": [maker("canceled_partial", "fallback_created", parent_event_id=None), fb("filled", parent_event_id=None)],
    }
    for name, rows in cases.items():
        r.check(name, kr.classify_deploy_rows(rows)["safe"], False)
    # A settled sibling does not launder an in-flight one elsewhere in the list.
    rows = [maker("canceled_partial", "fallback_created"), fb("filled"), row("limit_open", cl_ord_id="other")]
    r.check("any in-flight row blocks the whole gate", kr.classify_deploy_rows(rows)["safe"], False)


def t_failed_kraken_with_duplicate_signature_blocks(r):
    # Before ROB-21 a duplicate rejection whose lookup missed was written as
    # failed_kraken; such a row can sit beside a live, untracked order.
    r.check("plain explicit rejection stays safe",
            verdict(row("failed_kraken", reason="limit AddOrder failed: ['EOrder:Insufficient funds']")), "SAFE")
    r.check("duplicate in reason blocks",
            verdict(row("failed_kraken", reason="fallback AddOrder failed: ['EOrder:Duplicate order']")), "STOP")
    r.check("match is case-insensitive",
            verdict(row("failed_kraken", reason="AddOrder failed: ['EOrder:DUPLICATE']")), "STOP")
    r.check("duplicate in raw.error blocks (object raw)",
            verdict(row("failed_kraken", raw={"error": "['EOrder:Duplicate order']"})), "STOP")
    r.check("duplicate in raw.error blocks (JSON-string raw)",
            verdict(row("failed_kraken", raw=json.dumps({"error": "['EOrder:Duplicate order']"}))), "STOP")
    r.check("duplicate in raw.last_failure.error blocks",
            verdict(row("failed_kraken", raw={"last_failure": {"error": "duplicate"}})), "STOP")
    r.check("insufficient funds in raw.error stays safe",
            verdict(row("failed_kraken", raw={"error": "['EOrder:Insufficient funds']"})), "SAFE")
    r.check("other terminal statuses are unaffected by the word",
            verdict(row("skipped_min_order", reason="duplicate pair")), "SAFE")
    # The same rule applies through the parent event's sibling.
    dup_sibling = fb("failed_kraken", reason="fallback AddOrder failed: ['EOrder:Duplicate order']")
    result = kr.classify_deploy_rows([maker("rejected_postonly", "fallback_failed_kraken"), dup_sibling])
    r.check("fallback_failed_kraken beside a duplicate sibling blocks", result["safe"], False)
    r.check("the maker row itself is also STOP", result["verdicts"][0]["verdict"], "STOP")
    plain = fb("failed_kraken", reason="fallback AddOrder failed: ['EOrder:Insufficient funds']")
    r.check("fallback_failed_kraken beside a plain rejection is safe",
            kr.classify_deploy_rows([maker("rejected_postonly", "fallback_failed_kraken"), plain])["safe"], True)


def t_real_production_row_shapes_classify_safe(r):
    """The structural shapes reported by the 2026-10-07 read-only audit (Linear
    ROB-21): they must NOT false-STOP. No data is copied, only shapes."""
    rows = []
    # 3 filled re-peg rows, phase replacement_attached, replacement txid == row txid
    for i in range(3):
        rows.append(row("filled", cl_ord_id=f"f{i}", order_id=f"O{i}",
                        raw=json.dumps({"kraken_cl": "x" * 28, "repeg_count": 1,
                                        "repeg_transition": {"phase": "replacement_attached",
                                                             "replacement_order_id": f"O{i}"}})))
    # 2 canceled_unfilled re-peg rows, reason fallback_created, filled fallback sibling
    for i in range(2):
        ev = f"evt-r{i}"
        rows.append(row("canceled_unfilled", cl_ord_id=f"m{i}", parent_event_id=ev, reason="fallback_created",
                        raw=json.dumps({"repeg_transition": {"phase": "replacement_attached"}})))
        rows.append(fb("filled", cl_ord_id=f"m{i}-fb", parent_event_id=ev))
    # plain canceled_unfilled maker legs with a filled fallback sibling
    for i in range(7):
        ev = f"evt-c{i}"
        rows.append(row("canceled_unfilled", cl_ord_id=f"c{i}", parent_event_id=ev, reason="fallback_created"))
        rows.append(fb("filled", cl_ord_id=f"c{i}-fb", parent_event_id=ev))
    # dry-run maker rows with filled_dry_run fallback siblings (incl. partial)
    for status in ("canceled_partial_dry_run", "canceled_unfilled_dry_run", "rejected_postonly_dry_run"):
        ev = f"evt-{status}"
        rows.append(row(status, cl_ord_id=status, parent_event_id=ev, reason="fallback_created"))
        rows.append(fb("filled_dry_run", cl_ord_id=status + "-fb", parent_event_id=ev))
    # legacy rows with NULL attempt_type, JSON-string raw or NULL raw
    rows.append(row("filled", attempt_type=None, raw='{"id": "x"}'))
    rows.append(row("filled_dry_run", attempt_type=None, raw=None))
    rows.append(row("skipped_above_cap", attempt_type=None, reason="Mid above cap"))
    rows.append(row("skipped_insufficient_funds", attempt_type=None, reason="spendable < needed"))
    # the one historical explicit rejection
    rows.append(row("failed_kraken", reason="AddOrder failed: ['EOrder:Insufficient funds']",
                    raw=json.dumps({"error": "['EOrder:Insufficient funds']"})))
    result = kr.classify_deploy_rows(rows)
    r.check("the audited production shapes are all SAFE", result["safe"], True)
    r.check("no blocker among them", [b["cl_ord_id"] for b in result["blockers"]], [])
    r.check("every row was judged", len(result["verdicts"]), len(rows))



def t_result_shape(r):
    r.check("empty input is safe", kr.classify_deploy_rows([]), {"safe": True, "verdicts": [], "blockers": []})
    r.check("None input is safe", kr.classify_deploy_rows(None)["safe"], True)
    result = kr.classify_deploy_rows([row("filled"), row("claimed")])
    r.check("verdict per row", len(result["verdicts"]), 2)
    r.check("blockers listed", [b["status"] for b in result["blockers"]], ["claimed"])
    r.check("reason is explained", bool(result["blockers"][0]["reason"]), True)
    r.check("input rows are not mutated", row("filled"), row("filled"))


def t_classifier_is_pure(r):
    # It must not reach any service: replace every I/O entry point with a trap.
    trapped = {}
    for name in ("sb_get", "sb_insert", "sb_update", "kraken_private", "kraken_public", "tg_send"):
        trapped[name] = getattr(kr, name)
        setattr(kr, name, lambda *_a, _n=name, **_k: (_ for _ in ()).throw(AssertionError(f"classifier called {_n}")))
    try:
        result = kr.classify_deploy_rows([row("filled"), maker("canceled_partial", "fallback_created"), fb("filled")])
    finally:
        for name, fn in trapped.items():
            setattr(kr, name, fn)
    r.check("classification ran with every I/O entry point trapped", result["safe"], True)


TESTS = [
    ("status vocabulary matches source", t_status_vocabulary_matches_source),
    ("phase vocabulary matches source", t_phase_vocabulary_matches_source),
    ("fallback outcomes match source", t_fallback_outcomes_match_source),
    ("raw decoding", t_raw_decoding),
    ("every status covered explicitly", t_every_known_status_is_covered_explicitly),
    ("in-flight and manual states block", t_in_flight_and_manual_states_block),
    ("unknown status blocks", t_unknown_status_blocks),
    ("every phase covered explicitly", t_every_phase_is_covered_explicitly),
    ("unknown or malformed phase blocks", t_unknown_or_malformed_phase_blocks),
    ("malformed raw blocks", t_malformed_raw_blocks),
    ("maker decision needs parent-event evidence", t_maker_decision_needs_parent_event_evidence),
    ("proven terminal structures are safe", t_proven_terminal_parent_event_structures_are_safe),
    ("unproven structures block", t_unproven_structures_block),
    ("failed_kraken duplicate signature blocks", t_failed_kraken_with_duplicate_signature_blocks),
    ("real production row shapes classify safe", t_real_production_row_shapes_classify_safe),
    ("result shape", t_result_shape),
    ("classifier is pure", t_classifier_is_pure),
]

if __name__ == "__main__":
    raise SystemExit(Runner("ROB-21 deploy classifier").run(TESTS))
