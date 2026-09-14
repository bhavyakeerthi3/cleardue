"""Deterministic authority, project/milestone and freshness gates."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re

from .models import ConditionStatus, ReasoningProposal


def validate_assessment_authority(binding, assessment, evidence):
    """Recheck accepted conditions as time passes, without another model call."""
    proposal = ReasoningProposal(
        condition_evaluations=assessment["condition_registry"],
        claims=[], action_intents=[], human_dependencies=[], missing_evidence=[],
    )
    return validate_authority(binding, proposal, evidence)


def timestamp(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    return result if result.tzinfo is not None else None


def validate_authority(binding, proposal, evidence, now=None):
    now = now or datetime.now(timezone.utc)
    errors = []
    items = {e.evidence_id: e for e in evidence}
    for rule in binding.reviewed_conditions:
        condition = next((c for c in proposal.condition_evaluations if c.condition_id == rule["condition_id"]), None)
        if condition is None or condition.status != ConditionStatus.SATISFIED:
            continue
        scope = rule.get("scope")
        source = rule.get("source_app")
        if not scope or not source:
            errors.append(f"NEEDS_HUMAN: incomplete authority rule for {condition.condition_id}")
            continue
        refs = [items[r.evidence_id] for r in condition.support_refs if r.evidence_id in items and items[r.evidence_id].app == source]
        relevant = [e for e in evidence if e.app == source and not e.agent_generated and e.metadata.get("evidence_type") != "contract_clause" and scope.lower() in e.content_text.lower() and binding.project_ref.lower() in e.content_text.lower()]
        newest = max((timestamp(e.occurred_at) for e in relevant if timestamp(e.occurred_at)), default=None)
        valid = False
        for item in refs:
            when = timestamp(item.occurred_at)
            after = timestamp(rule.get("fresh_after"))
            if item.agent_generated or item not in relevant or not when:
                continue
            if when > now or (now-when).total_seconds() > rule.get("max_age_seconds", 2592000) or after and when < after:
                continue
            if newest and when < newest:
                continue
            if source == "gmail":
                # Conservative negative/hedged acceptance veto, independent of model status.
                if re.search(r"\b(failed|withdrawn|probably|maybe|cannot accept|not accepted|not approved)\b", item.content_text, re.I):
                    continue
                cited_text = " ".join(r.quote for r in condition.support_refs if r.evidence_id == item.evidence_id)
                if not re.search(r"\b(accept|accepted|approve|approved|give written acceptance)\b", cited_text, re.I):
                    continue
                sender = item.identity.get("from", "").lower()
                permitted = any(p.email.lower() == sender and p.role == "authorized_customer_approver" and scope in p.scope for p in binding.participant_roles)
                if not permitted:
                    continue
            if source == "jira" and item.identity.get("project_key") != binding.jira_project_id:
                continue
            valid = True
        if not valid:
            errors.append(f"NEEDS_HUMAN: no current authorized {scope} evidence for {condition.condition_id}")
    return errors
