from __future__ import annotations

import json

from app.config import Settings
from app.models import CaseBinding, EvidenceItem
from app.policy import build_plan
from app.reasoning import FixtureReasoner


def test_live_configuration_rejects_unbounded_evidence_scope(tmp_path):
    google_secret = tmp_path / "google.json"
    google_secret.write_text("{}", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        CLEARDUE_MODE="live",
        GEMINI_API_KEY="test-key",
        GOOGLE_CLIENT_SECRET_FILE=google_secret,
        JIRA_BASE_URL="https://example.atlassian.net",
        JIRA_EMAIL="operator@example.com",
        JIRA_API_TOKEN="test-token",
        JIRA_PROJECT_KEY="KAN",
        RAZORPAY_KEY_ID="test-id",
        RAZORPAY_KEY_SECRET="test-secret",
        DEMO_INVOICE_ID="inv_live",
        DEMO_APPROVER_EMAIL="",
        DEMO_PROJECT_REF="",
        DEMO_JIRA_ISSUE_IDS="",
    )

    assert settings.live_missing() == [
        "DEMO_APPROVER_EMAIL",
        "DEMO_PROJECT_REF",
        "DEMO_JIRA_ISSUE_IDS",
    ]


def test_live_jira_action_uses_bound_kan_project_and_ignores_model_target():
    from app.config import ROOT

    fixture = json.loads(
        (ROOT / "evals" / "fixtures" / "main_case.json").read_text(encoding="utf-8")
    )
    proposal, _ = FixtureReasoner().reason({"evidence": fixture["evidence"]})
    jira_intent = next(
        intent for intent in proposal.action_intents
        if intent.allowed_type == "CREATE_JIRA_REMEDIATION"
    )
    jira_intent.target_binding_key = "CD"
    binding = CaseBinding.model_validate(fixture["binding"]).model_copy(
        update={"jira_project_id": "KAN", "bound_issue_ids": ["KAN-2"]}
    )

    plan, errors = build_plan(
        binding,
        proposal,
        [EvidenceItem.model_validate(item) for item in fixture["evidence"]],
        "fingerprint",
    )

    assert errors == []
    jira_action = next(action for action in plan.actions if action.app == "jira")
    assert jira_action.exact_target_id == "KAN"
    assert jira_action.exact_target_id != jira_intent.target_binding_key
    assert jira_action.payload["summary"].strip()
    assert jira_action.payload["description"].strip()


def test_jira_remediation_expands_empty_model_content_from_validated_context():
    from app.config import ROOT

    fixture = json.loads(
        (ROOT / "evals" / "fixtures" / "main_case.json").read_text(encoding="utf-8")
    )
    proposal, _ = FixtureReasoner().reason({"evidence": fixture["evidence"]})
    jira_intent = next(
        intent for intent in proposal.action_intents
        if intent.allowed_type == "CREATE_JIRA_REMEDIATION"
    )
    jira_intent.content_fields = {"summary": "  ", "description": "\n"}
    binding = CaseBinding.model_validate(fixture["binding"]).model_copy(
        update={"jira_project_id": "KAN", "project_ref": "CLEARDUE-LIVE-2026-09-14"}
    )

    plan, errors = build_plan(
        binding,
        proposal,
        [EvidenceItem.model_validate(item) for item in fixture["evidence"]],
        "fingerprint",
    )

    assert errors == []
    jira_action = next(action for action in plan.actions if action.app == "jira")
    assert jira_action.exact_target_id == "KAN"
    assert jira_action.payload["summary"].strip()
    assert jira_action.payload["description"].strip()
    assert "CLEARDUE-LIVE-2026-09-14" in jira_action.payload["summary"]
    assert "migration-acceptance" in jira_action.payload["description"]
