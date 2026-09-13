from __future__ import annotations

import base64
import hashlib
import json
from email import policy as email_policy
from email.parser import BytesParser
from typing import Any

from .actions import ActionExecutor
from .adapters.gmail import GmailAdapter
from .adapters.jira import JiraAdapter
from .adapters.razorpay import RazorpayAdapter
from .config import Settings
from .conditions import live_reviewed_condition_registry
from .db import Database
from .evidence import canonicalize_evidence_text
from .fixtures import FixtureProviderSuite
from .models import CaseBinding, EvidenceItem, FinancialSnapshot, SourceManifest, utc_now
from .policy import build_plan, readiness_for, stable_hash, validate_identity
from .reasoning import FixtureReasoner, GeminiReasoner, PROMPT_VERSION


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _gmail_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in {"text/plain", "text/html"}:
            text = _b64(data).decode("utf-8", errors="replace")
            if mime == "text/plain":
                chunks.append(text)
        for child in part.get("parts", []):
            walk(child)
    walk(payload)
    return canonicalize_evidence_text("\n".join(chunks))


def _adf_text(value: Any) -> str:
    if isinstance(value, dict):
        own = value.get("text", "")
        return own + " ".join(_adf_text(child) for child in value.get("content", []))
    if isinstance(value, list):
        return " ".join(_adf_text(item) for item in value)
    return ""


class WorkflowEngine:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.fixture = FixtureProviderSuite() if settings.mode == "fixture" else None
        self.executor = ActionExecutor(db, settings, self.fixture)
        self.reasoner = FixtureReasoner() if settings.mode == "fixture" else GeminiReasoner(settings.gemini_api_key or "", settings.gemini_model)

    def enqueue(self, case_id: str, event_type: str, source_key: str, payload: dict[str, Any]) -> tuple[str, bool]:
        dedupe = hashlib.sha256(f"{case_id}|{event_type}|{source_key}".encode()).hexdigest()
        return self.db.insert_event_once(case_id, "operator", dedupe, event_type, payload)

    def process_next(self) -> bool:
        event = self.db.due_event()
        if not event:
            return False
        try:
            if event["event_type"] in {"INVESTIGATE", "REFRESH"}:
                self.investigate(event["case_id"])
            self.db.complete_event(event["id"], event["case_id"], "AWAITING_APPROVAL")
        except Exception as exc:
            self.db.update_case_state(event["case_id"], "NEEDS_OPERATOR", readiness="UNKNOWN", reason_codes=[type(exc).__name__])
            with self.db.transaction() as conn:
                conn.execute("UPDATE events SET status='FAILED', error_json=? WHERE id=?", (json.dumps({"type": type(exc).__name__, "message": str(exc)[:500]}), event["id"]))
            raise
        return True

    def investigate(self, case_id: str) -> str:
        case = self.db.get_case(case_id)
        if not case:
            raise KeyError(case_id)
        binding = CaseBinding.model_validate(case["binding"])
        if not self.fixture and binding.jira_project_id != self.settings.jira_project_key:
            raise ValueError(
                "live CaseBinding Jira project does not match configured JIRA_PROJECT_KEY"
            )
        financial, evidence, manifests = self._collect(binding)
        identity_errors = validate_identity(binding, evidence, financial.customer_id)
        for item in evidence:
            self.db.save_evidence(case_id, item.model_dump(mode="json"))
        evidence_payload = [item.model_dump(mode="json") for item in evidence if not item.agent_generated]
        reviewed_registry = [] if self.fixture else live_reviewed_condition_registry(financial)
        bundle = {
            "case": binding.model_dump(mode="json"),
            "financial": financial.model_dump(mode="json"),
            "evidence": evidence_payload,
            "source_manifests": [item.model_dump(mode="json") for item in manifests],
            "reviewed_condition_registry": reviewed_registry,
        }
        input_fingerprint = stable_hash(bundle)
        proposal, usage = self.reasoner.reason(bundle)
        plan, grounding_errors = build_plan(
            binding, proposal, evidence, input_fingerprint,
            [line.model_dump(mode="json") for line in financial.lines],
            reviewed_registry,
        )
        errors = identity_errors + grounding_errors
        readiness, reasons = readiness_for(proposal, [m.model_dump(mode="json") for m in manifests], financial.model_dump(mode="json"), errors)
        return self.db.save_assessment(case_id, {
            "input_fingerprint": input_fingerprint,
            "source_manifest": [m.model_dump(mode="json") for m in manifests],
            "condition_registry": [c.model_dump(mode="json") for c in proposal.condition_evaluations],
            "decisions": [d.model_dump(mode="json") for d in plan.line_assessments],
            "claims": [c.model_dump(mode="json") for c in proposal.claims],
            "plan": {**plan.model_dump(mode="json"), "root_cause": proposal.root_cause.model_dump(mode="json") if proposal.root_cause else None},
            "plan_hash": plan.plan_hash, "model_id": self.reasoner.model_id, "prompt_version": PROMPT_VERSION,
            "validation": {"valid": not errors, "errors": errors}, "usage": usage,
            "readiness": readiness, "reason_codes": reasons, "financial": financial.model_dump(mode="json"),
        })

    def approve_and_execute(self, case_id: str, assessment_id: str, plan_hash: str, operator: str) -> list[str]:
        case = self.db.get_case(case_id)
        assessment = self.db.latest_assessment(case_id)
        if not case or not assessment or assessment["id"] != assessment_id:
            raise ValueError("assessment is stale")
        if not assessment["validation"].get("valid"):
            raise ValueError("assessment failed deterministic validation")
        approval = {"operator": operator, "approved_at": utc_now(), "plan_hash": plan_hash, "condition_registry_hash": assessment["plan"]["condition_registry_hash"], "binding_version": case["binding_version"]}
        if not self.db.approve_assessment(case_id, assessment_id, plan_hash, approval):
            raise ValueError("approval hash or current assessment mismatch")
        action_ids = self.executor.reserve_plan(case, assessment)
        for action_id in action_ids:
            self.executor.execute(action_id)
        actions = self.db.list_actions(case_id)
        status = "IDLE" if all(a["verification_status"] == "VERIFIED" for a in actions) else "RECOVERING"
        self.db.update_case_state(case_id, status)
        return action_ids

    def _collect(self, binding: CaseBinding) -> tuple[FinancialSnapshot, list[EvidenceItem], list[SourceManifest]]:
        if self.fixture:
            financial, evidence, manifests = self.fixture.collect()
            return financial, [EvidenceItem.model_validate(item) for item in evidence], [SourceManifest.model_validate(item) for item in manifests]
        return self._collect_live(binding)

    def _collect_live(self, binding: CaseBinding) -> tuple[FinancialSnapshot, list[EvidenceItem], list[SourceManifest]]:
        gmail = GmailAdapter(self.settings.google_token_file, binding.mailbox_ref)
        jira = JiraAdapter(self.settings.jira_base_url or "", self.settings.jira_email or "", self.settings.jira_api_token or "", binding.jira_project_id, self.settings.jira_issue_type)
        razor = RazorpayAdapter(self.settings.razorpay_key_id or "", self.settings.razorpay_key_secret or "", binding.payment_account_ref)
        financial = razor.get_invoice(binding.invoice_id)
        evidence: list[EvidenceItem] = []
        query = f'"{binding.project_ref}" ({" OR ".join("from:" + p.email for p in binding.participant_roles)})'
        message_ids, truncated = gmail.search_messages(query, cap=100)
        for hit in message_ids:
            message = gmail.get_message(hit["id"])
            headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
            text = _gmail_text(message.get("payload", {}))
            sender = headers.get("from", "")
            sender_email = sender.rsplit("<", 1)[-1].rstrip(">").strip().lower()
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            evidence.append(EvidenceItem(evidence_id=f"ev-gmail-{hit['id']}-{content_hash[:8]}", app="gmail", account_ref=binding.mailbox_ref, external_id=hit["id"], source_version=message.get("historyId", content_hash), occurred_at=headers.get("date"), retrieved_at=utc_now(), identity={"from": sender_email, "project_ref": binding.project_ref}, content_text=text, locator={"message_id": hit["id"], "thread_id": message.get("threadId"), "part": "text/plain"}, metadata={"subject": headers.get("subject", ""), "evidence_type": "customer_communication"}, content_hash=content_hash))
        for issue_id in binding.bound_issue_ids:
            issue = jira.get_issue(issue_id)
            fields = issue["fields"]
            text = canonicalize_evidence_text(
                f"{issue['key']} {fields.get('summary', '')}. Status: {fields.get('status', {}).get('name', '')}. {_adf_text(fields.get('description'))}"
            )
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            evidence.append(EvidenceItem(evidence_id=f"ev-jira-{binding.jira_project_id}-{issue['id']}-{content_hash[:8]}", app="jira", account_ref=binding.jira_project_id, external_id=issue["key"], source_version=fields.get("updated", content_hash), retrieved_at=utc_now(), identity={"project_key": fields.get("project", {}).get("key"), "project_ref": binding.project_ref}, content_text=text, locator={"issue_key": issue["key"]}, metadata={"evidence_type": "delivery_record"}, content_hash=content_hash))
        now = utc_now()
        manifests = [
            SourceManifest(app="gmail", account_ref=binding.mailbox_ref, fetched_at=now, status="INCOMPLETE" if truncated else "COMPLETE", scope_description=f"Project reference {binding.project_ref}, approved participants, cap 100", complete=not truncated, truncated=truncated, source_object_ids=[x["id"] for x in message_ids]),
            SourceManifest(app="jira", account_ref=binding.jira_project_id, fetched_at=now, status="COMPLETE", scope_description="Bound issues", complete=True, truncated=False, source_object_ids=binding.bound_issue_ids),
            SourceManifest(app="razorpay", account_ref=binding.payment_account_ref, fetched_at=now, status="COMPLETE", scope_description="Bound invoice", complete=True, truncated=False, source_object_ids=[binding.invoice_id]),
        ]
        if not evidence:
            raise ValueError(
                "bounded live evidence is empty; configure DEMO_PROJECT_REF, "
                "DEMO_APPROVER_EMAIL, and DEMO_JIRA_ISSUE_IDS for existing live evidence"
            )
        return financial, evidence, manifests
