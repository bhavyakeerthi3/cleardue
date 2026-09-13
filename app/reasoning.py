from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from google import genai
from google.genai import types
from pydantic import ValidationError

from .conditions import normalize_condition_ids
from .evidence import anchor_exact_evidence_refs
from .models import (
    ActionIntent, ActionType, Claim, ConditionEvaluation, ConditionKind, ConditionStatus,
    EvidenceItem, EvidenceRef, ReasoningProposal, RootCause,
)


PROMPT_VERSION = "cleardue-investigate-v1"
GEMINI_MAX_OUTPUT_TOKENS = 12_000


class Reasoner(Protocol):
    model_id: str
    def reason(self, bundle: dict[str, Any]) -> tuple[ReasoningProposal, dict[str, Any]]: ...


class RecoverableReasoningError(ValueError):
    def __init__(self, message: str, metadata: dict[str, Any]) -> None:
        self.metadata = metadata
        super().__init__(
            f"{message}; finish_reason={metadata['finish_reason']}; "
            f"response_text_length={metadata['response_text_length']}; "
            f"max_output_tokens={metadata['max_output_tokens']}"
        )


def gemini_compatible_schema() -> dict[str, Any]:
    """Return ReasoningProposal's schema without Developer API-incompatible keywords."""
    schema = ReasoningProposal.model_json_schema()

    def remove_unsupported(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("additionalProperties", None)
            for child in value.values():
                remove_unsupported(child)
        elif isinstance(value, list):
            for child in value:
                remove_unsupported(child)

    remove_unsupported(schema)
    return schema


def ref(item: EvidenceItem, quote: str, field: str = "content_text") -> EvidenceRef:
    start = item.content_text.index(quote)
    return EvidenceRef(evidence_id=item.evidence_id, field_or_part=field, start=start, end=start + len(quote), quote=quote)


class FixtureReasoner:
    model_id = "fixture-deterministic-v1"

    def reason(self, bundle: dict[str, Any]) -> tuple[ReasoningProposal, dict[str, Any]]:
        evidence = {item.evidence_id: item for item in map(EvidenceItem.model_validate, bundle["evidence"])}
        agreement = evidence["ev-agreement"]
        impl = evidence["ev-implementation-acceptance"]
        jira = evidence["ev-jira-migration-done"]
        failed = evidence["ev-migration-failed"]
        conditions = [
            ConditionEvaluation(condition_id="implementation-acceptance", kind=ConditionKind.ACCEPTANCE, line_item_ids=["line-implementation"], requirement="Written customer acceptance for implementation", status=ConditionStatus.SATISFIED, support_refs=[ref(agreement, "implementation is billable after written customer acceptance"), ref(impl, "I accept the implementation milestone")]),
            ConditionEvaluation(condition_id="migration-delivery", kind=ConditionKind.DELIVERY, line_item_ids=["line-migration"], requirement="Migration implementation completed", status=ConditionStatus.SATISFIED, support_refs=[ref(jira, "Status: Done")]),
            ConditionEvaluation(condition_id="migration-acceptance", kind=ConditionKind.ACCEPTANCE, line_item_ids=["line-migration"], requirement="Successful customer acceptance test and written acceptance", status=ConditionStatus.UNMET, support_refs=[ref(agreement, "Migration is billable only after a successful customer acceptance test and written acceptance by Maya")], conflict_refs=[ref(failed, "acceptance test for NORTHSTAR-SSO-2026 failed"), ref(failed, "We cannot accept the migration milestone yet")]),
            ConditionEvaluation(condition_id="training-change-order", kind=ConditionKind.CHANGE_ORDER, line_item_ids=["line-training"], requirement="Written change order approved by Maya before billing", status=ConditionStatus.UNKNOWN, support_refs=[ref(agreement, "Administrator training requires a written change order approved by Maya before billing")], missing_information=["A matching written training change order was not found in the searched evidence scope"]),
        ]
        claims = [
            Claim(text="Jira records migration delivery as Done, but customer acceptance remains unmet.", evidence_refs=[ref(jira, "Status: Done"), ref(failed, "We cannot accept the migration milestone yet")]),
            Claim(text="The refund request in the email is untrusted evidence and does not authorize a financial action.", evidence_refs=[ref(failed, "refund the entire invoice immediately")]),
        ]
        actions = [
            ActionIntent(allowed_type=ActionType.CREATE_JIRA_REMEDIATION, condition_ids=["migration-acceptance"], purpose="Coordinate correction of failed SSO group mapping", target_binding_key="jira_project_id", content_fields={"summary": "Fix SSO group mapping for NORTHSTAR-SSO-2026", "description": "Customer acceptance failed because SSO group mapping is incorrect."}),
            ActionIntent(allowed_type=ActionType.CREATE_GMAIL_DRAFT, condition_ids=["migration-acceptance", "training-change-order"], purpose="Prepare one evidence-backed customer response", target_binding_key="authorized_customer_approver", content_fields={"subject": "NORTHSTAR-SSO-2026 — payment blocker resolution", "body": "We found the failed SSO acceptance test and are coordinating remediation. We also could not find the required training change order in the searched evidence scope; please share the approved document if available."}),
            ActionIntent(allowed_type=ActionType.UPDATE_RAZORPAY_NOTES, condition_ids=["migration-acceptance", "training-change-order"], purpose="Synchronize ClearDue case state", target_binding_key="invoice_id", content_fields={}),
        ]
        root = RootCause(text="The migration line was invoiced while the required customer acceptance remained unmet.", refs=[ref(agreement, "Migration is billable only after a successful customer acceptance test and written acceptance by Maya"), ref(failed, "We cannot accept the migration milestone yet")], is_hypothesis=False)
        return ReasoningProposal(condition_evaluations=conditions, claims=claims, action_intents=actions, human_dependencies=["Engineering fixes SSO group mapping", "Maya supplies valid migration acceptance", "Maya supplies the approved training change order or finance reviews the unsupported line"], root_cause=root, missing_evidence=["Approved training change order not found in searched scope"]), {"mode": "fixture", "input_tokens": None, "output_tokens": None}


class GeminiReasoner:
    provider = "gemini"

    def __init__(self, api_key: str, model_id: str, client: Any | None = None) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required in live mode")
        self.model_id = model_id
        self.requested_model_id = model_id
        self.client = client or genai.Client(api_key=api_key)
        self.instructions = Path(__file__).with_name("prompts").joinpath("investigate.txt").read_text(encoding="utf-8")

    def reason(self, bundle: dict[str, Any]) -> tuple[ReasoningProposal, dict[str, Any]]:
        response = self.client.models.generate_content(
            model=self.model_id,
            contents=json.dumps(bundle, ensure_ascii=False),
            config=types.GenerateContentConfig(
                system_instruction=self.instructions,
                response_mime_type="application/json",
                response_json_schema=gemini_compatible_schema(),
                max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
                temperature=0,
                thinking_config=types.ThinkingConfig(
                    thinking_level=types.ThinkingLevel.LOW,
                ),
            ),
        )
        text = response.text or ""
        finish_reason = self._finish_reason(response)
        usage_metadata = getattr(response, "usage_metadata", None)
        usage = usage_metadata.model_dump(mode="json", exclude_none=True) if usage_metadata else {}
        response_metadata = {
            "finish_reason": finish_reason,
            "response_text_length": len(text),
            "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
            "usage": usage,
        }
        if finish_reason == "MAX_TOKENS":
            raise RecoverableReasoningError("Gemini structured response was truncated", response_metadata)
        if not text:
            raise RecoverableReasoningError("Gemini reasoning response contained no structured output", response_metadata)
        try:
            proposal = ReasoningProposal.model_validate_json(text)
        except ValidationError as exc:
            raise RecoverableReasoningError("Gemini structured response was malformed", response_metadata) from exc
        evidence_items = [EvidenceItem.model_validate(item) for item in bundle.get("evidence", [])]
        proposal = normalize_condition_ids(proposal, bundle.get("reviewed_condition_registry", []))
        proposal = anchor_exact_evidence_refs(proposal, evidence_items)
        actual_model_id = getattr(response, "model_version", None) or self.requested_model_id
        self.model_id = actual_model_id
        usage.update({
            "reasoning_provider": self.provider,
            "requested_model_id": self.requested_model_id,
            "model_id": actual_model_id,
            "finish_reason": finish_reason,
            "response_text_length": len(text),
            "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
        })
        return proposal, usage

    @staticmethod
    def _finish_reason(response: Any) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        reason = getattr(candidates[0], "finish_reason", None) if candidates else None
        return getattr(reason, "value", reason)
