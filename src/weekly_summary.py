"""Pure Weekly Summary read model. Never writes execution evidence.

The caller owns the unchanged reporting window. No clock is read here: both
current-day and older unresolved parents block, irrespective of their age.
"""
import json
import math


POLICY_SKIPS = {
    "skipped_insufficient_funds", "skipped_above_cap",
    "skipped_target_too_small", "skipped_min_order",
}
FAILURES = {"failed_kraken", "failed_reconciliation", "manual_required"}
IN_FLIGHT = {"claimed", "placed", "limit_open", "repeg_recovery_pending"}
CLOSED_LEGS = {"canceled_unfilled", "rejected_postonly"}


def _raw(row):
    raw = row.get("raw") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("invalid raw evidence")
    return raw


def _dry(row):
    # Check cheap, explicit provenance before parsing any other evidence.
    if (row.get("status") or "").lower().endswith("_dry_run"):
        return True
    if "-dry" in (row.get("cl_ord_id") or "").lower():
        return True
    return _raw(row).get("dry_run") in (True, "true", "True", 1)


def _terminal_failure(row):
    """Only known no-order terminal writers prove a failed real purchase.

    manual_required can hide live exposure; zero numeric fields do not settle
    that. Unknown Kraken errors (including duplicate/transport uncertainty)
    also need adjudication, rather than a guess based on their status.
    """
    if row.get("order_id") or not row.get("execution_finished_at"):
        return False
    reason = row.get("reason") or ""
    if row["status"] == "failed_reconciliation":
        return reason == "Claimed but no Kraken order found after timeout"
    if row["status"] != "failed_kraken":
        return False
    if reason.startswith("AssetPairs lookup failed:") or reason in {
        "No valid BID price – cannot compute base volume",
        "No valid ASK price – cannot compute base volume",
    }:
        return True
    # Explicit exchange rejections, as stored by _failure_raw; no order exists.
    return _raw(row).get("error") in {
        "EOrder:Insufficient funds", "EOrder:Order minimum not met",
        "EOrder:Cost minimum not met", "EOrder:Invalid price",
        "EOrder:Invalid volume", "EGeneral:Invalid arguments",
    }


def _parent(rows):
    statuses = {r.get("status") for r in rows}
    if len({r.get("pair") for r in rows}) != 1 or not rows[0].get("pair"):
        return None, "inconsistent-pair parents"
    if statuses & IN_FLIGHT:
        return None, "unresolved parents"
    # Partial completion has no approved Step-1 interpretation. Even a filled
    # sibling alone does not prove which exposure it settled.
    if "canceled_partial" in statuses:
        return None, "unadjudicated-partial parents"
    if not statuses <= POLICY_SKIPS | FAILURES | CLOSED_LEGS | {"filled"}:
        return None, "unknown-status parents"

    amounts = []
    for row in rows:
        values = tuple(float(row.get(k) or 0) for k in (
            "filled_quote_cost", "fee_quote", "filled_base_volume"))
        if any(not math.isfinite(v) or v < 0 for v in values):
            return None, "invalid-economics parents"
        amounts.append(values)
    if any(row.get("status") in CLOSED_LEGS and any(values)
           for row, values in zip(rows, amounts)):
        return None, "ambiguous-purchase parents"
    cost, fee, volume = (sum(v[i] for v in amounts) for i in range(3))
    economic = [(r, v) for r, v in zip(rows, amounts) if any(v)]
    if economic:
        if (statuses & FAILURES or "filled" not in statuses
                or any(v[0] <= 0 or v[2] <= 0 for _, v in economic)
                or any(r.get("status") != "filled" for r, _ in economic)):
            return None, "ambiguous-purchase parents"
        # A filled status with no purchase amounts is inconsistent evidence.
        if any(r.get("status") == "filled" and (v[0] <= 0 or v[2] <= 0)
               for r, v in zip(rows, amounts)):
            return None, "invalid-fill parents"
        outcome = "filled"
    elif "filled" in statuses:
        return None, "invalid-fill parents"
    elif statuses & POLICY_SKIPS and not statuses & FAILURES:
        outcome = "skipped"
    elif statuses & FAILURES and not statuses & POLICY_SKIPS and all(
            _terminal_failure(r) for r in rows if r.get("status") in FAILURES):
        outcome = "failed"
    else:
        return None, "unresolved-or-ambiguous parents"

    # Preserve the existing arithmetic mean of purchase-row (avg-mid)/mid.
    # No parent weighting or new benchmark is invented. Numeric columns already
    # contain generation fills; raw.generation_fills is deliberately not added.
    slippages = []
    for row, _ in economic:
        mid, avg = float(row.get("mid") or 0), float(row.get("avg_price") or 0)
        if not math.isfinite(mid) or not math.isfinite(avg):
            return None, "invalid-slippage parents"
        if mid > 0 and avg > 0:
            slippages.append((avg - mid) / mid * 100)
    return (outcome, cost, fee, volume, slippages), None


def weekly_summary_evidence(rows):
    """Return (pair totals, blocker details); blockers suppress ALL totals.

    Null parents are counted as rows (no invented event identity). Every other
    blocker is counted once per parent. Each detail contains its count and the
    sorted unique source dates classified in the same pass. Dry evidence is
    excluded before either.
    """
    parents, blockers, pairs = {}, {}, {}

    def add_blocker(reason, blocked_rows):
        detail = blockers.setdefault(reason, {"count": 0, "dates": set()})
        detail["count"] += 1
        detail["dates"].update(
            row["trade_date_chicago"] for row in blocked_rows
            if row.get("trade_date_chicago")
        )

    for row in rows:
        try:
            if _dry(row):
                continue
        except (TypeError, ValueError):
            add_blocker("invalid-provenance rows", [row])
            continue
        parent = row.get("parent_event_id")
        if parent is None:
            add_blocker("null-parent rows", [row])
        else:
            parents.setdefault(parent, []).append(row)
    for rows in parents.values():
        try:
            result, blocker = _parent(rows)
        except (TypeError, ValueError, OverflowError):
            result, blocker = None, "invalid-evidence parents"
        if blocker:
            add_blocker(blocker, rows)
            continue
        outcome, cost, fee, volume, slips = result
        stats = pairs.setdefault(rows[0]["pair"], {
            "filled": 0, "skipped": 0, "failed": 0,
            "total_cost": 0, "total_fee": 0, "total_vol": 0, "slippages": [],
        })
        stats[outcome] += 1
        stats["total_cost"] += cost
        stats["total_fee"] += fee
        stats["total_vol"] += volume
        stats["slippages"].extend(slips)
    blocker_details = {
        reason: {"count": detail["count"], "dates": sorted(detail["dates"])}
        for reason, detail in blockers.items()
    }
    return ({} if blockers else pairs), blocker_details
