from __future__ import annotations

import json

from app.config import ROOT
from app.models import ActionIntent, ActionType, CaseBinding, EvidenceItem
from app.policy import build_plan, validate_identity, validate_evidence_refs, readiness_for
from app.reasoning import FixtureReasoner


def load_fixture():
    return json.loads((ROOT / "evals" / "fixtures" / "main_case.json").read_text(encoding="utf-8"))


def test_conflict_remains_blocked_and_refund_is_not_an_action():
    fixture = load_fixture()
    proposal, _ = FixtureReasoner().reason({"evidence": fixture["evidence"]})
    statuses = {c.condition_id: c.status for c in proposal.condition_evaluations}
    assert statuses["migration-delivery"] == "SATISFIED"
    assert statuses["migration-acceptance"] == "UNMET"
    assert {a.allowed_type for a in proposal.action_intents} == {
        ActionType.CREATE_GMAIL_DRAFT, ActionType.CREATE_JIRA_REMEDIATION, ActionType.UPDATE_RAZORPAY_NOTES
    }


def test_invalid_evidence_reference_is_rejected():
    fixture = load_fixture()
    proposal, _ = FixtureReasoner().reason({"evidence": fixture["evidence"]})
    proposal.claims[0].evidence_refs[0].evidence_id = "invented"
    _, errors = build_plan(CaseBinding.model_validate(fixture["binding"]), proposal, [EvidenceItem.model_validate(x) for x in fixture["evidence"]], "fingerprint")
    assert any("unknown" in error for error in errors)


def test_similar_name_never_overrides_customer_id():
    fixture = load_fixture()
    binding = CaseBinding.model_validate(fixture["binding"])
    errors = validate_identity(binding, [EvidenceItem.model_validate(x) for x in fixture["evidence"]], "cust_northstar_similar_999")
    assert "Razorpay customer does not match bound customer" in errors


def test_uncited_satisfied_condition_cannot_make_invoice_ready():
    fixture = load_fixture()
    proposal, _ = FixtureReasoner().reason({"evidence": fixture["evidence"]})
    for condition in proposal.condition_evaluations:
        condition.status = "SATISFIED"
        condition.support_refs = []
        condition.conflict_refs = []
    errors = validate_evidence_refs(
        proposal, [EvidenceItem.model_validate(x) for x in fixture["evidence"]]
    )
    assert errors
    state, _ = readiness_for(proposal, fixture["manifests"], fixture["financial"], errors)
    assert state == "BLOCKED"


def test_material_claim_without_citations_is_rejected():
    fixture = load_fixture()
    proposal, _ = FixtureReasoner().reason({"evidence": fixture["evidence"]})
    proposal.claims[0].evidence_refs = []
    assert "material claim has no evidence references" in validate_evidence_refs(
        proposal, [EvidenceItem.model_validate(x) for x in fixture["evidence"]]
    )
