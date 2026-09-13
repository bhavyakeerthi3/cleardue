from __future__ import annotations

from app.conditions import normalize_condition_ids, validate_reviewed_condition_registry
from app.models import (
    ActionIntent,
    ActionType,
    ConditionEvaluation,
    ConditionKind,
    ConditionStatus,
    ReasoningProposal,
)


REGISTRY = [{
    "condition_id": "cond-acceptance-001",
    "kind": "ACCEPTANCE",
    "line_item_ids": ["line-live-1"],
    "requirement": "Successful customer acceptance before billing",
}]


def proposal(condition_id: str, kind: ConditionKind = ConditionKind.ACCEPTANCE) -> ReasoningProposal:
    return ReasoningProposal(
        condition_evaluations=[ConditionEvaluation(
            condition_id=condition_id,
            kind=kind,
            line_item_ids=["line-live-1"],
            requirement="Customer acceptance is required",
            status=ConditionStatus.UNMET,
        )],
        claims=[],
        action_intents=[ActionIntent(
            allowed_type=ActionType.CREATE_JIRA_REMEDIATION,
            condition_ids=[condition_id],
            purpose="Resolve acceptance blocker",
            target_binding_key="jira_project_id",
            content_fields={},
        )],
        human_dependencies=[],
        missing_evidence=[],
    )


def test_numeric_condition_alias_maps_to_stable_reviewed_id() -> None:
    normalized = normalize_condition_ids(proposal("cond-acceptance-1"), REGISTRY)
    assert normalized.condition_evaluations[0].condition_id == "cond-acceptance-001"
    assert normalized.action_intents[0].condition_ids == ["cond-acceptance-001"]
    assert validate_reviewed_condition_registry(normalized, REGISTRY) == []


def test_unknown_condition_id_is_rejected() -> None:
    candidate = normalize_condition_ids(proposal("new-acceptance-condition"), REGISTRY)
    errors = validate_reviewed_condition_registry(candidate, REGISTRY)
    assert any("unknown or semantically invalid" in error for error in errors)
    assert any("missing reviewed condition IDs" in error for error in errors)


def test_alias_with_wrong_semantics_is_not_mapped() -> None:
    candidate = normalize_condition_ids(
        proposal("cond-acceptance-1", kind=ConditionKind.DELIVERY), REGISTRY
    )
    assert candidate.condition_evaluations[0].condition_id == "cond-acceptance-1"
    assert any(
        "unknown or semantically invalid" in error
        for error in validate_reviewed_condition_registry(candidate, REGISTRY)
    )


def test_ambiguous_registry_is_rejected() -> None:
    ambiguous = REGISTRY + [{**REGISTRY[0]}]
    errors = validate_reviewed_condition_registry(proposal("cond-acceptance-001"), ambiguous)
    assert errors == ["ambiguous reviewed condition registry"]
