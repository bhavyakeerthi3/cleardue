from __future__ import annotations

import re
from typing import Any

from .models import FinancialSnapshot, ReasoningProposal


LIVE_ACCEPTANCE_CONDITION_ID = "cond-acceptance-001"


def live_reviewed_condition_registry(financial: FinancialSnapshot) -> list[dict[str, Any]]:
    """Return the frozen conditions reviewed for the live demo invoice."""
    return [{
        "condition_id": LIVE_ACCEPTANCE_CONDITION_ID,
        "kind": "ACCEPTANCE",
        "line_item_ids": [line.id for line in financial.lines],
        "requirement": "Successful customer acceptance test and written acceptance prior to billing",
    }]


def _condition_id_key(condition_id: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"(.+?)-(\d+)", condition_id.strip().lower())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def normalize_condition_ids(
    proposal: ReasoningProposal, registry: list[dict[str, Any]]
) -> ReasoningProposal:
    """Resolve model aliases only against one unambiguous semantic registry entry."""
    normalized = proposal.model_copy(deep=True)
    aliases: dict[str, str] = {}
    for condition in normalized.condition_evaluations:
        candidates = [
            entry for entry in registry
            if _condition_id_key(entry["condition_id"]) == _condition_id_key(condition.condition_id)
            and entry["kind"] == condition.kind.value
            and set(entry["line_item_ids"]) == set(condition.line_item_ids)
        ]
        if len(candidates) == 1:
            stable_id = candidates[0]["condition_id"]
            aliases[condition.condition_id] = stable_id
            condition.condition_id = stable_id

    for intent in normalized.action_intents:
        intent.condition_ids = [aliases.get(condition_id, condition_id) for condition_id in intent.condition_ids]
    return normalized


def validate_reviewed_condition_registry(
    proposal: ReasoningProposal, registry: list[dict[str, Any]]
) -> list[str]:
    if not registry:
        return []
    errors: list[str] = []
    registry_ids = [entry["condition_id"] for entry in registry]
    if len(registry_ids) != len(set(registry_ids)):
        errors.append("ambiguous reviewed condition registry")
        return errors

    evaluated_ids = [condition.condition_id for condition in proposal.condition_evaluations]
    for condition in proposal.condition_evaluations:
        matches = [
            entry for entry in registry
            if entry["condition_id"] == condition.condition_id
            and entry["kind"] == condition.kind.value
            and set(entry["line_item_ids"]) == set(condition.line_item_ids)
        ]
        if len(matches) != 1:
            errors.append(f"unknown or semantically invalid condition ID: {condition.condition_id}")
    if len(evaluated_ids) != len(set(evaluated_ids)):
        errors.append("duplicate condition ID")
    missing = sorted(set(registry_ids) - set(evaluated_ids))
    if missing:
        errors.append("missing reviewed condition IDs: " + ", ".join(missing))
    return errors
