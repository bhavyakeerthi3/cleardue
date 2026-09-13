from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    IDLE = "IDLE"
    RECOVERING = "RECOVERING"
    NEEDS_OPERATOR = "NEEDS_OPERATOR"


class Readiness(StrEnum):
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    READY_FOR_PAYMENT = "READY_FOR_PAYMENT"
    CLOSED = "CLOSED"


class ConditionKind(StrEnum):
    DELIVERY = "DELIVERY"
    ACCEPTANCE = "ACCEPTANCE"
    CHANGE_ORDER = "CHANGE_ORDER"
    DOCUMENTATION = "DOCUMENTATION"
    PAYMENT_TIMING = "PAYMENT_TIMING"
    BILLING_AUTHORITY = "BILLING_AUTHORITY"


class ConditionStatus(StrEnum):
    SATISFIED = "SATISFIED"
    UNMET = "UNMET"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class LineDecision(StrEnum):
    SUPPORTED = "SUPPORTED"
    BLOCKED = "BLOCKED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ActionType(StrEnum):
    CREATE_GMAIL_DRAFT = "CREATE_GMAIL_DRAFT"
    CREATE_JIRA_REMEDIATION = "CREATE_JIRA_REMEDIATION"
    UPDATE_RAZORPAY_NOTES = "UPDATE_RAZORPAY_NOTES"


class RequestStatus(StrEnum):
    PLANNED = "PLANNED"
    IN_FLIGHT = "IN_FLIGHT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNCERTAIN = "UNCERTAIN"
    RETRY_WAIT = "RETRY_WAIT"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class VerificationStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class ParticipantRole(StrictModel):
    email: str
    role: str
    scope: list[str]


class CaseBinding(StrictModel):
    case_id: str
    binding_version: int = 1
    payment_account_ref: str
    invoice_id: str
    customer_id: str
    mailbox_ref: str
    participant_roles: list[ParticipantRole]
    jira_project_id: str
    project_ref: str
    bound_issue_ids: list[str] = Field(default_factory=list)
    contract_evidence_ids: list[str] = Field(default_factory=list)


class LineItem(StrictModel):
    id: str
    description: str
    amount_minor: int
    quantity: int = 1


class FinancialSnapshot(StrictModel):
    invoice_id: str
    customer_id: str
    currency: str
    amount_minor: int
    amount_paid_minor: int
    amount_due_minor: int
    provider_status: str
    lines: list[LineItem]
    notes: dict[str, str] = Field(default_factory=dict)
    financial_fingerprint: str
    fetched_at: str
    raw_accounting: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_amounts(self) -> "FinancialSnapshot":
        if any(value < 0 for value in (self.amount_minor, self.amount_paid_minor, self.amount_due_minor)):
            raise ValueError("financial amounts must be non-negative")
        if sum(line.amount_minor for line in self.lines) != self.amount_minor:
            raise ValueError("line total does not equal invoice amount")
        if self.amount_paid_minor + self.amount_due_minor != self.amount_minor:
            raise ValueError("paid plus due does not equal invoice amount")
        return self


class EvidenceRef(StrictModel):
    evidence_id: str
    field_or_part: str
    start: int
    end: int
    quote: str


class EvidenceItem(StrictModel):
    evidence_id: str
    app: Literal["gmail", "jira", "razorpay", "fixture"]
    account_ref: str
    external_id: str
    source_version: str
    occurred_at: str | None = None
    retrieved_at: str
    identity: dict[str, Any]
    content_text: str
    locator: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    agent_generated: bool = False


class SourceManifest(StrictModel):
    app: str
    account_ref: str
    fetched_at: str
    status: Literal["COMPLETE", "INCOMPLETE", "UNAVAILABLE"]
    scope_description: str
    complete: bool
    truncated: bool
    source_object_ids: list[str]
    error: str | None = None


class ConditionEvaluation(StrictModel):
    condition_id: str
    kind: ConditionKind
    line_item_ids: list[str]
    requirement: str
    status: ConditionStatus
    support_refs: list[EvidenceRef] = Field(default_factory=list)
    conflict_refs: list[EvidenceRef] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class Claim(StrictModel):
    text: str
    evidence_refs: list[EvidenceRef]


class ActionIntent(StrictModel):
    allowed_type: ActionType
    condition_ids: list[str]
    purpose: str
    target_binding_key: str
    content_fields: dict[str, Any]
    depends_on_purposes: list[str] = Field(default_factory=list)


class RootCause(StrictModel):
    text: str
    refs: list[EvidenceRef]
    is_hypothesis: bool = True


class ReasoningProposal(StrictModel):
    condition_evaluations: list[ConditionEvaluation]
    claims: list[Claim]
    action_intents: list[ActionIntent]
    human_dependencies: list[str]
    root_cause: RootCause | None = None
    missing_evidence: list[str] = Field(default_factory=list)


class LineAssessment(StrictModel):
    line_item_id: str
    decision: LineDecision
    condition_ids: list[str]
    explanation: str


class PlannedAction(StrictModel):
    action_type: ActionType
    app: Literal["gmail", "jira", "razorpay"]
    logical_target: str
    purpose_revision: str
    condition_ids: list[str]
    exact_target_id: str
    payload: dict[str, Any]
    dependencies: list[str] = Field(default_factory=list)


class ValidatedPlan(StrictModel):
    case_id: str
    input_fingerprint: str
    condition_registry_hash: str
    line_assessments: list[LineAssessment]
    actions: list[PlannedAction]
    awaited_conditions: list[str]
    plan_hash: str
    review_reasons: list[str]


class WriteOutcome(StrictModel):
    disposition: Literal["ACKNOWLEDGED", "DEFINITELY_REJECTED", "UNCERTAIN"]
    external_id: str | None = None
    external_url: str | None = None
    provider_request_id: str | None = None
    error: str | None = None


class ReconcileOutcome(StrictModel):
    disposition: Literal[
        "FOUND_MATCH", "NOT_YET_FOUND", "FOUND_MISMATCH", "MULTIPLE_MATCHES", "UNAVAILABLE"
    ]
    candidate_ids: list[str]
    verified_fields: dict[str, Any]
    checked_at: str


class Verification(StrictModel):
    status: VerificationStatus
    observed_external_id: str | None
    expected_fields_hash: str
    observed_fields: dict[str, Any]
    evidence_of_readback: dict[str, Any]
    checked_at: str


class ApprovalRequest(StrictModel):
    assessment_id: str
    plan_hash: str


class FaultRequest(StrictModel):
    provider: Literal["jira"]
    fault: Literal["lose_create_response"]

