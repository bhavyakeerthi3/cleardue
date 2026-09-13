from __future__ import annotations

import hashlib
import json
from typing import Any

from .conditions import validate_reviewed_condition_registry
from .models import (
    ActionIntent, ActionType, CaseBinding, ConditionKind, ConditionStatus, EvidenceItem, LineAssessment,
    LineDecision, PlannedAction, ReasoningProposal, ValidatedPlan,
)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def validate_evidence_refs(proposal: ReasoningProposal, evidence_items: list[EvidenceItem]) -> list[str]:
    evidence = {item.evidence_id: item for item in evidence_items if not item.agent_generated}
    errors: list[str] = []
    refs = []
    for condition in proposal.condition_evaluations:
        if condition.status == ConditionStatus.SATISFIED and not condition.support_refs:
            errors.append(f"satisfied condition has no supporting evidence: {condition.condition_id}")
        elif condition.status in {ConditionStatus.UNMET, ConditionStatus.CONFLICT} and not (condition.support_refs or condition.conflict_refs):
            errors.append(f"condition decision has no evidence: {condition.condition_id}")
        refs.extend(condition.support_refs)
        refs.extend(condition.conflict_refs)
    for claim in proposal.claims:
        if not claim.evidence_refs:
            errors.append("material claim has no evidence references")
        refs.extend(claim.evidence_refs)
    if proposal.root_cause:
        refs.extend(proposal.root_cause.refs)
    for ref in refs:
        item = evidence.get(ref.evidence_id)
        if not item:
            errors.append(f"unknown or agent-generated evidence: {ref.evidence_id}")
            continue
        if ref.field_or_part != "content_text":
            errors.append(f"invalid evidence field: {ref.evidence_id}")
            continue
        if ref.start < 0 or ref.end > len(item.content_text) or ref.start >= ref.end:
            errors.append(f"invalid span: {ref.evidence_id}")
            continue
        if item.content_text[ref.start:ref.end] != ref.quote:
            errors.append(f"quote mismatch: {ref.evidence_id}")
    return errors


def validate_identity(binding: CaseBinding, evidence_items: list[EvidenceItem], customer_id: str) -> list[str]:
    errors: list[str] = []
    if customer_id != binding.customer_id:
        errors.append("Razorpay customer does not match bound customer")
    approved = {p.email.lower() for p in binding.participant_roles}
    for item in evidence_items:
        if item.app == "gmail" and item.identity.get("from"):
            sender = str(item.identity["from"]).lower()
            if item.metadata.get("evidence_type") in {"customer_acceptance", "contract_clause"} and sender not in approved:
                errors.append(f"unapproved authoritative sender: {sender}")
        project = item.identity.get("project_ref")
        if project and project != binding.project_ref:
            errors.append(f"wrong project evidence: {item.evidence_id}")
    return errors


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _jira_remediation_payload(
    binding: CaseBinding,
    intent: ActionIntent,
    conditions: dict[str, Any],
    evidence_items: list[EvidenceItem],
) -> tuple[dict[str, str], str]:
    related = [conditions[condition_id] for condition_id in intent.condition_ids if condition_id in conditions]
    purpose = _clean_text(intent.purpose)
    supplied_summary = _clean_text(intent.content_fields.get("summary"))
    if supplied_summary:
        summary = supplied_summary
    elif purpose:
        summary = f"[{binding.project_ref}] {purpose}"
    else:
        kinds = sorted({condition.kind.value.lower() for condition in related})
        summary = f"[{binding.project_ref}] Resolve {'/'.join(kinds) or 'payment'} blocker"
    summary = summary[:255].rstrip()

    supplied_description = str(intent.content_fields.get("description") or "").strip()
    if supplied_description:
        description = supplied_description
    else:
        evidence = {item.evidence_id: item for item in evidence_items if not item.agent_generated}
        excerpts: list[str] = []
        for condition in related:
            for ref in condition.support_refs + condition.conflict_refs:
                if ref.evidence_id in evidence and ref.quote not in excerpts:
                    excerpts.append(ref.quote)
        # Include bounded acceptance records so remediation details remain grounded
        # even when the condition cites only the customer's final rejection line.
        if any(condition.kind == ConditionKind.ACCEPTANCE for condition in related):
            for item in evidence_items:
                text = item.content_text
                if "accept" in text.lower() and text not in excerpts:
                    excerpts.append(text[:600])
                if len(excerpts) >= 4:
                    break
        requirements = [
            f"{condition.condition_id}: {_clean_text(condition.requirement)}"
            for condition in related
        ]
        lines = [
            f"Project reference: {binding.project_ref}",
            f"Remediation purpose: {purpose or 'Resolve the validated payment blocker'}",
            "Validated condition(s): " + "; ".join(requirements),
        ]
        if excerpts:
            lines.append("Bounded evidence:\n- " + "\n- ".join(excerpts))
        if any(condition.kind == ConditionKind.ACCEPTANCE for condition in related):
            lines.append(
                "Required outcome: correct the delivery issue, run a new customer acceptance test, "
                "and obtain written acceptance."
            )
        description = "\n\n".join(lines).strip()

    payload = {"summary": summary, "description": description}
    purpose_revision = stable_hash({
        "condition_ids": sorted(intent.condition_ids),
        "summary": summary,
        "description": description,
    })[:16]
    return payload, purpose_revision


def build_plan(
    binding: CaseBinding,
    proposal: ReasoningProposal,
    evidence_items: list[EvidenceItem],
    input_fingerprint: str,
    line_items: list[dict[str, Any]] | None = None,
    reviewed_condition_registry: list[dict[str, Any]] | None = None,
) -> tuple[ValidatedPlan, list[str]]:
    errors = validate_evidence_refs(proposal, evidence_items)
    errors.extend(validate_reviewed_condition_registry(proposal, reviewed_condition_registry or []))
    condition_ids = {condition.condition_id for condition in proposal.condition_evaluations}
    conditions = {condition.condition_id: condition for condition in proposal.condition_evaluations}
    if len(condition_ids) != len(proposal.condition_evaluations) and "duplicate condition ID" not in errors:
        errors.append("duplicate condition ID")
    for intent in proposal.action_intents:
        if any(condition_id not in condition_ids for condition_id in intent.condition_ids):
            errors.append(f"action references unknown condition: {intent.purpose}")

    line_map = {item["id"]: item.get("description", item["id"]) for item in line_items} if line_items else {
        "line-implementation": "Implementation milestone",
        "line-migration": "SSO migration milestone",
        "line-training": "Administrator training",
    }
    line_assessments: list[LineAssessment] = []
    for line_id, label in line_map.items():
        related = [c for c in proposal.condition_evaluations if line_id in c.line_item_ids]
        statuses = {c.status for c in related}
        if ConditionStatus.UNKNOWN in statuses or ConditionStatus.CONFLICT in statuses:
            decision = LineDecision.REVIEW_REQUIRED
        elif ConditionStatus.UNMET in statuses:
            decision = LineDecision.BLOCKED
        elif related and statuses == {ConditionStatus.SATISFIED}:
            decision = LineDecision.SUPPORTED
        else:
            decision = LineDecision.REVIEW_REQUIRED
        line_assessments.append(LineAssessment(line_item_id=line_id, decision=decision, condition_ids=[c.condition_id for c in related], explanation=f"{label}: {decision.value.replace('_', ' ').lower()} from {len(related)} reviewed condition(s)."))

    actions: list[PlannedAction] = []
    approver = next((p.email for p in binding.participant_roles if p.role == "authorized_customer_approver"), None)
    revision = stable_hash(sorted(condition_ids))[:12]
    for intent in proposal.action_intents:
        if intent.allowed_type == ActionType.CREATE_GMAIL_DRAFT:
            if not approver:
                errors.append("no bound authorized customer approver")
                continue
            payload = {"to": approver, "subject": intent.content_fields.get("subject", ""), "body": intent.content_fields.get("body", "")}
            actions.append(PlannedAction(action_type=intent.allowed_type, app="gmail", logical_target=f"draft:{intent.purpose}", purpose_revision=revision, condition_ids=intent.condition_ids, exact_target_id=approver, payload=payload))
        elif intent.allowed_type == ActionType.CREATE_JIRA_REMEDIATION:
            payload, jira_revision = _jira_remediation_payload(
                binding, intent, conditions, evidence_items
            )
            if not payload["summary"] or not payload["description"]:
                errors.append(f"Jira remediation content is empty: {intent.purpose}")
                continue
            actions.append(PlannedAction(action_type=intent.allowed_type, app="jira", logical_target="remediation:migration-acceptance", purpose_revision=jira_revision, condition_ids=intent.condition_ids, exact_target_id=binding.jira_project_id, payload=payload))
        elif intent.allowed_type == ActionType.UPDATE_RAZORPAY_NOTES:
            actions.append(PlannedAction(action_type=intent.allowed_type, app="razorpay", logical_target=f"invoice:{binding.invoice_id}:case-state", purpose_revision=revision, condition_ids=intent.condition_ids, exact_target_id=binding.invoice_id, payload={"cleardue_case": binding.case_id, "cleardue_state": "BLOCKED", "cleardue_revision": "1"}))
        else:
            errors.append(f"forbidden action type: {intent.allowed_type}")

    # One action per business effect, ordered by remediation, communication, bookkeeping.
    order = {ActionType.CREATE_JIRA_REMEDIATION: 0, ActionType.CREATE_GMAIL_DRAFT: 1, ActionType.UPDATE_RAZORPAY_NOTES: 2}
    deduped: dict[tuple[ActionType, str], PlannedAction] = {}
    for action in actions:
        deduped[(action.action_type, action.logical_target)] = action
    actions = sorted(deduped.values(), key=lambda a: order[a.action_type])
    condition_registry_hash = stable_hash([c.model_dump(mode="json") for c in proposal.condition_evaluations])
    base = {
        "case_id": binding.case_id,
        "input_fingerprint": input_fingerprint,
        "condition_registry_hash": condition_registry_hash,
        "line_assessments": [x.model_dump(mode="json") for x in line_assessments],
        "actions": [x.model_dump(mode="json") for x in actions],
        "awaited_conditions": proposal.human_dependencies,
        "review_reasons": [reason for c in proposal.condition_evaluations if c.status in {ConditionStatus.UNKNOWN, ConditionStatus.CONFLICT} for reason in c.missing_information],
    }
    plan_hash = stable_hash(base)
    plan = ValidatedPlan(**base, plan_hash=plan_hash)
    return plan, errors


def effect_key(app: str, account_ref: str, case_id: str, action: PlannedAction) -> str:
    raw = "|".join([app, account_ref, case_id, action.action_type, action.logical_target, action.purpose_revision])
    return hashlib.sha256(raw.encode()).hexdigest()


def readiness_for(proposal: ReasoningProposal, manifests: list[dict[str, Any]], financial: dict[str, Any], validation_errors: list[str]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if validation_errors:
        reasons.append("VALIDATION_REVIEW")
    if any(not item.get("complete") or item.get("truncated") for item in manifests):
        reasons.append("SOURCE_INCOMPLETE")
    provider_status = financial["provider_status"].lower()
    if provider_status in {"paid", "cancelled", "expired", "deleted"}:
        return "CLOSED", [provider_status.upper()]
    if provider_status not in {"issued", "partially_paid"} or financial["amount_due_minor"] <= 0:
        reasons.append("FINANCIAL_STATE_NOT_PAYABLE")
    for condition in proposal.condition_evaluations:
        if condition.status != ConditionStatus.SATISFIED:
            reasons.append(f"{condition.condition_id}:{condition.status}")
    return ("READY_FOR_PAYMENT", []) if not reasons else ("BLOCKED", sorted(set(reasons)))
