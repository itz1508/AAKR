"""PRE signals and applicability evaluation.

Catalog stores conditions and group definitions, not task-specific result
states. PRE is the Runtime consumer that derives local signals with cited
evidence and evaluates each condition in three-valued logic:
  satisfied, not_satisfied, evidence_unavailable.

absent requires positively scoped complete evidence of absence.
Missing signal, missing file, no search hits, and unsupported operator
are NOT satisfied — they yield evidence_unavailable.

Signal registry is versioned. Use example.* namespace for fixtures only.
Do not invent production signal names while actual PRE is unavailable.
"""

from __future__ import annotations

from typing import Any

from aakr.catalog.models import ALLOWED_EVIDENCE, ALLOWED_GROUPS, CatalogError

ALLOWED_OPERATORS = {"eq", "ne", "contains", "exists", "absent"}
VALID_OUTCOMES = {"satisfied", "not_satisfied", "evidence_unavailable"}


def evaluate_condition(
    operator: str,
    expected: str | None,
    observation: dict[str, Any] | None,
    *,
    evidence_kind: str,
) -> str:
    """Evaluate a single condition against a PRE observation.

    Returns one of: satisfied, not_satisfied, evidence_unavailable.

    Rules:
    - Unsupported operator/evidence_kind -> evidence_unavailable
    - Missing observation, wrong evidence_kind, missing evidence_ref -> evidence_unavailable
    - absent: satisfied only if complete=True and present=False
    - exists: satisfied if present=True; not_satisfied if complete=True and present=False
    - eq/ne/contains: requires present=True and string value
    """
    if evidence_kind not in ALLOWED_EVIDENCE or operator not in ALLOWED_OPERATORS:
        return "evidence_unavailable"
    if (
        not observation
        or observation.get("evidence_kind") != evidence_kind
        or not observation.get("evidence_ref")
    ):
        return "evidence_unavailable"

    if operator == "absent":
        if observation.get("complete") is True and observation.get("present") is False:
            return "satisfied"
        if observation.get("present") is True:
            return "not_satisfied"
        return "evidence_unavailable"

    if operator == "exists":
        if observation.get("present") is True:
            return "satisfied"
        if observation.get("complete") is True and observation.get("present") is False:
            return "not_satisfied"
        return "evidence_unavailable"

    if observation.get("present") is not True or not isinstance(
        observation.get("value"), str
    ):
        return "evidence_unavailable"

    observed = observation["value"]
    if operator == "eq":
        matches = observed == expected
    elif operator == "ne":
        matches = observed != expected
    elif operator == "contains":
        matches = isinstance(expected, str) and expected in observed
    else:
        matches = False
    return "satisfied" if matches else "not_satisfied"


def evaluate_group(mode: str, child_results: list[str]) -> str:
    """Evaluate a group of condition results using three-valued logic.

    | Group mode | satisfied | not_satisfied | evidence_unavailable |
    |---|---|---|---|
    | all_of | Every child satisfied | At least one child not satisfied | No false child and >=1 unknown |
    | any_of | At least one child satisfied | Every child not satisfied | No true child and >=1 unknown |
    | none_of | Every child not satisfied | At least one child satisfied | No true child and >=1 unknown |

    Empty groups are invalid (raise CatalogError).
    """
    if mode not in ALLOWED_GROUPS:
        raise CatalogError("invalid applicability group")
    if not child_results or any(x not in VALID_OUTCOMES for x in child_results):
        raise CatalogError("invalid applicability group")

    if mode == "all_of":
        if "not_satisfied" in child_results:
            return "not_satisfied"
        if "evidence_unavailable" in child_results:
            return "evidence_unavailable"
        return "satisfied"

    if mode == "any_of":
        if "satisfied" in child_results:
            return "satisfied"
        if "evidence_unavailable" in child_results:
            return "evidence_unavailable"
        return "not_satisfied"

    # none_of
    if "satisfied" in child_results:
        return "not_satisfied"
    if "evidence_unavailable" in child_results:
        return "evidence_unavailable"
    return "satisfied"


def evaluate_entry(
    groups: list[dict[str, Any]],
    conditions: list[dict[str, Any]],
    observations: dict[str, dict[str, Any]],
    *,
    evidence_kinds: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate all applicability groups for an entry version.

    Returns evaluation trace with per-condition, per-group, and overall results.
    Zero groups mean no applicability declared (not automatic compatibility).
    """
    evidence_kinds = evidence_kinds or {}
    trace = []
    overall = "satisfied"

    for group in groups:
        group_id = group["group_id"]
        group_mode = group["group_mode"]
        group_conditions = [c for c in conditions if c["group_id"] == group_id]
        if not group_conditions:
            raise CatalogError(f"empty group {group_id}")

        child_results = []
        for cond in group_conditions:
            obs = observations.get(cond["condition_id"])
            ek = evidence_kinds.get(cond["condition_id"], cond["required_evidence"])
            result = evaluate_condition(
                cond["operator"], cond.get("value"), obs, evidence_kind=ek
            )
            child_results.append(result)
            trace.append(
                {
                    "condition_id": cond["condition_id"],
                    "signal": cond["signal"],
                    "operator": cond["operator"],
                    "expected_value": cond.get("value"),
                    "result": result,
                    "group_id": group_id,
                }
            )

        group_result = evaluate_group(group_mode, child_results)
        trace.append(
            {
                "group_id": group_id,
                "group_mode": group_mode,
                "child_results": child_results,
                "group_result": group_result,
            }
        )
        if group_result == "not_satisfied":
            overall = "not_satisfied"
        elif group_result == "evidence_unavailable" and overall != "not_satisfied":
            overall = "evidence_unavailable"

    return {
        "overall_result": overall if groups else "not_evaluated",
        "trace": trace,
    }
