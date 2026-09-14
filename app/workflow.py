from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from email import policy as email_policy
from email.utils import parseaddr
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
        self.fixture = FixtureProviderSuite(settings.db_path.with_suffix(".providers.json")) if settings.mode == "fixture" else None
        if self.fixture and settings.db_path.with_suffix(".sources.json").exists():
            self.fixture.source_path = settings.db_path.with_suffix(".sources.json")
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
            if event["event_type"] in {"INVESTIGATE", "REFRESH", "EVIDENCE_CHANGED"}:
                self.investigate(event["case_id"], previous_readiness=event.get("payload", {}).get("previous_readiness"))
            current = self.db.get_case(event["case_id"])
            self.db.complete_event(event["id"], event["case_id"], current["workflow_status"])
        except Exception as exc:
            self.db.update_case_state(event["case_id"], "NEEDS_OPERATOR", readiness="UNKNOWN", reason_codes=[type(exc).__name__])
            with self.db.transaction() as conn:
                conn.execute("UPDATE events SET status='FAILED', error_json=? WHERE id=?", (json.dumps({"type": type(exc).__name__, "message": str(exc)[:500]}), event["id"]))
            raise
        return True

    def investigate(self, case_id: str, previous_readiness: str | None = None) -> str:
        case = self.db.get_case(case_id)
        if not case:
            raise KeyError(case_id)
        binding = CaseBinding.model_validate(case["binding"])
        if not self.fixture and binding.jira_project_id != self.settings.jira_project_key:
            raise ValueError(
                "live CaseBinding Jira project does not match configured JIRA_PROJECT_KEY"
            )
        financial, evidence, manifests = self._collect(binding)
        for manifest in manifests:
            self.db.record_activity(case_id, "TOOL_COMPLETED", {
                "tool": manifest.app, "mode": self.settings.mode,
                "source_complete": manifest.complete and not manifest.truncated,
                "source_object_count": len(manifest.source_object_ids),
                "fetched_at": manifest.fetched_at,
            })
        identity_errors = validate_identity(binding, evidence, financial.customer_id)
        if financial.invoice_id != binding.invoice_id:
            identity_errors.append("Invoice ID differs from the reviewed binding")
        if binding.expected_amount_minor is not None and binding.expected_amount_minor != financial.amount_minor:
            identity_errors.append("NEEDS_HUMAN: amount differs from reviewed invoice")
        if binding.expected_currency and binding.expected_currency != financial.currency:
            identity_errors.append("NEEDS_HUMAN: currency differs from reviewed invoice")
        for item in evidence:
            self.db.save_evidence(case_id, item.model_dump(mode="json"))
        evidence_payload = [item.model_dump(mode="json") for item in evidence if not item.agent_generated]
        reviewed_registry = binding.reviewed_conditions or ([] if self.fixture else live_reviewed_condition_registry(financial))
        bundle = {
            "case": binding.model_dump(mode="json"),
            "financial": financial.model_dump(mode="json"),
            "evidence": evidence_payload,
            "source_manifests": [item.model_dump(mode="json") for item in manifests],
            "reviewed_condition_registry": reviewed_registry,
        }
        input_fingerprint = self.snapshot_fingerprint(binding, financial, evidence, manifests)
        proposal, usage = self.reasoner.reason(bundle)
        plan, grounding_errors = build_plan(
            binding, proposal, evidence, input_fingerprint,
            [line.model_dump(mode="json") for line in financial.lines],
            reviewed_registry,
        )
        errors = identity_errors + grounding_errors
        if binding.reviewed_conditions:
            covered = {line_id for c in binding.reviewed_conditions for line_id in c["line_item_ids"]}
            if covered != {line.id for line in financial.lines}:
                errors.append("NEEDS_HUMAN: reviewed conditions do not cover exactly the invoice lines")
            from .authority import validate_authority
            errors += validate_authority(binding, proposal, evidence)
            if any(c.status == "CONFLICT" for c in proposal.condition_evaluations):
                errors.append("NEEDS_HUMAN: conflicting acceptance requires a human decision")
            from .minimal_plan import select_minimum
            plan.actions, plan.alternatives = select_minimum(plan.actions, proposal)
            if any("No candidate plan" in a["reason"] for a in plan.alternatives):
                errors.append("NEEDS_HUMAN: proposed actions do not cover required work")
            plan.plan_hash = stable_hash(plan.model_dump(mode="json", exclude={"plan_hash"}))
        if errors:
            plan.actions = []
            plan.review_reasons = sorted(set(plan.review_reasons + errors))
            plan.plan_hash = stable_hash(plan.model_dump(mode="json", exclude={"plan_hash"}))
        readiness, reasons = readiness_for(proposal, [m.model_dump(mode="json") for m in manifests], financial.model_dump(mode="json"), errors)
        assessment_id = self.db.save_assessment(case_id, {
            "input_fingerprint": input_fingerprint,
            "source_manifest": [m.model_dump(mode="json") for m in manifests],
            "condition_registry": [c.model_dump(mode="json") for c in proposal.condition_evaluations],
            "decisions": [d.model_dump(mode="json") for d in plan.line_assessments],
            "claims": [c.model_dump(mode="json") for c in proposal.claims],
            "plan": {**plan.model_dump(mode="json"), "root_cause": proposal.root_cause.model_dump(mode="json") if proposal.root_cause else None},
            "plan_hash": plan.plan_hash, "model_id": usage.get("model_id") or self.reasoner.model_id, "prompt_version": PROMPT_VERSION,
            "validation": {"valid": not errors, "errors": errors}, "usage": usage,
            "readiness": readiness, "reason_codes": reasons, "financial": financial.model_dump(mode="json"),
        })
        self.db.record_activity(case_id, "RE_EVALUATED", {"before": previous_readiness or case["readiness"], "after": readiness, "assessment_id": assessment_id, "reasons": reasons, "mode": self.settings.mode})
        self.db.update_case_state(case_id, "NEEDS_OPERATOR" if errors else "AWAITING_APPROVAL" if plan.actions else "IDLE")
        return assessment_id

    def approve_and_execute(self, case_id: str, assessment_id: str, plan_hash: str, operator: str) -> list[str]:
        case = self.db.get_case(case_id)
        assessment = self.db.latest_assessment(case_id)
        if not case or not assessment or assessment["id"] != assessment_id:
            raise ValueError("assessment is stale")
        if not assessment["validation"].get("valid"):
            raise ValueError("assessment failed deterministic validation")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(assessment["created_at"])).total_seconds()
        if age > self.settings.approval_ttl_seconds:
            raise ValueError("assessment is stale: approval time window expired")
        binding = CaseBinding.model_validate(case["binding"])
        fresh = self._collect(binding)
        if self.snapshot_fingerprint(binding, *fresh) != assessment["input_fingerprint"]:
            self.db.update_case_state(case_id, "NEEDS_OPERATOR", readiness="UNKNOWN", reason_codes=["STALE_APPROVAL_NEW_EVIDENCE"])
            raise ValueError("assessment is stale: provider evidence changed")
        from .authority import validate_assessment_authority
        if validate_assessment_authority(binding, assessment, fresh[1]):
            self.db.update_case_state(case_id, "NEEDS_OPERATOR", readiness="UNKNOWN", reason_codes=["ACCEPTANCE_AUTHORITY_EXPIRED"])
            raise ValueError("assessment is stale: acceptance evidence no longer satisfies authority/freshness requirements")
        approval = {"operator": operator, "approved_at": utc_now(), "plan_hash": plan_hash, "condition_registry_hash": assessment["plan"]["condition_registry_hash"], "binding_version": case["binding_version"]}
        if not self.db.approve_assessment(case_id, assessment_id, plan_hash, approval):
            raise ValueError("approval hash or current assessment mismatch")
        action_ids = self.executor.reserve_plan(case, assessment)
        for action_id in action_ids:
            self.executor.execute(action_id)
        actions = [self.db.get_action(action_id) for action_id in action_ids]
        status = "IDLE" if all(a["verification_status"] == "VERIFIED" for a in actions) else "RECOVERING"
        self.db.update_case_state(case_id, status)
        return action_ids

    @staticmethod
    def snapshot_fingerprint(binding, financial, evidence, manifests):
        money = financial.model_dump(mode="json", exclude={"fetched_at", "notes", "financial_fingerprint", "raw_accounting"})
        return stable_hash({"binding": binding.model_dump(mode="json"), "financial": money,
            "evidence": sorted((e.app, e.external_id, e.content_hash, e.source_version) for e in evidence if not e.agent_generated),
            "sources": sorted((m.app, m.complete, m.truncated, m.status) for m in manifests)})

    def poll_evidence(self, case_id: str) -> tuple[str | None, bool]:
        case = self.db.get_case(case_id)
        assessment = self.db.latest_assessment(case_id)
        if not assessment or case["workflow_status"] in {"RUNNING", "QUEUED"}:
            return None, False
        binding = CaseBinding.model_validate(case["binding"])
        try:
            fresh = self._collect(binding)
            current = self.snapshot_fingerprint(binding, *fresh)
        except Exception:
            self.db.update_case_state(case_id, "NEEDS_OPERATOR", readiness="UNKNOWN", reason_codes=["SOURCE_REFRESH_FAILED"])
            raise
        if current == assessment["input_fingerprint"]:
            from .authority import validate_assessment_authority
            if case["readiness"] == "READY_FOR_PAYMENT" and validate_assessment_authority(binding, assessment, fresh[1]):
                self.db.update_case_state(case_id, "NEEDS_OPERATOR", readiness="UNKNOWN", reason_codes=["ACCEPTANCE_AUTHORITY_EXPIRED"])
                self.db.record_activity(case_id, "READINESS_INVALIDATED", {"before": "READY_FOR_PAYMENT", "after": "UNKNOWN", "reason": "Acceptance evidence expired; human review required"})
            return None, False
        self.db.update_case_state(case_id, "QUEUED", readiness="UNKNOWN", reason_codes=["NEW_EVIDENCE_REQUIRES_REEVALUATION"])
        return self.enqueue(case_id, "EVIDENCE_CHANGED", current, {"previous_assessment": assessment["id"], "previous_readiness": case["readiness"]})

    def _collect_source(self, binding, source):
        if self.fixture:
            financial, evidence, manifests = self._collect(binding)
            return financial if source == "razorpay" else None, [e for e in evidence if e.app == source], [m for m in manifests if m.app == source]
        return self._collect_live(binding, only=source)

    def _collect(self, binding: CaseBinding) -> tuple[FinancialSnapshot, list[EvidenceItem], list[SourceManifest]]:
        if self.fixture:
            financial, evidence, manifests = self.fixture.collect()
            return financial, [EvidenceItem.model_validate(item) for item in evidence], [SourceManifest.model_validate(item) for item in manifests]
        return self._collect_live(binding)

    def _collect_live(self, binding: CaseBinding, only: str | None = None) -> tuple[FinancialSnapshot, list[EvidenceItem], list[SourceManifest]]:
        gmail = GmailAdapter(self.settings.google_token_file, binding.mailbox_ref)
        jira = JiraAdapter(self.settings.jira_base_url or "", self.settings.jira_email or "", self.settings.jira_api_token or "", binding.jira_project_id, self.settings.jira_issue_type)
        razor = RazorpayAdapter(self.settings.razorpay_key_id or "", self.settings.razorpay_key_secret or "", binding.payment_account_ref)
        financial = razor.get_invoice(binding.invoice_id) if only in (None, "razorpay") else None
        evidence: list[EvidenceItem] = []
        jira_truncated = False
        query = f'"{binding.project_ref}" -in:drafts -in:sent'
        message_ids, truncated = gmail.search_messages(query, cap=30) if only in (None, "gmail") else ([], False)
        for hit in message_ids:
            message = gmail.get_message(hit["id"])
            headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
            text = _gmail_text(message.get("payload", {}))
            if len(text) > 12000:
                truncated = True
                text = text[:12000]
            sender = headers.get("from", "")
            sender_email = parseaddr(sender)[1].lower()
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            evidence.append(EvidenceItem(evidence_id=f"ev-gmail-{hit['id']}-{content_hash[:8]}", app="gmail", account_ref=binding.mailbox_ref, external_id=hit["id"], source_version=message.get("historyId", content_hash), occurred_at=datetime.fromtimestamp(int(message["internalDate"])/1000, timezone.utc).isoformat() if message.get("internalDate") else headers.get("date"), retrieved_at=utc_now(), identity={"from": sender_email, "project_ref": binding.project_ref}, content_text=text, locator={"message_id": hit["id"], "thread_id": message.get("threadId"), "part": "text/plain"}, metadata={"subject": headers.get("subject", ""), "evidence_type": "customer_communication"}, content_hash=content_hash, agent_generated=bool(headers.get("x-cleardue-operation")) or "DRAFT" in message.get("labelIds", [])))
        for issue_id in (binding.bound_issue_ids if only in (None, "jira") else []):
            issue = jira.get_issue(issue_id)
            fields = issue["fields"]
            comments = jira.get_comments(issue_id)
            jira_truncated = jira_truncated or len(comments) >= 100
            comment_text = "\n".join(_adf_text(c.get("body")) for c in comments)
            text = canonicalize_evidence_text(
                f"{issue['key']} {fields.get('summary', '')}. Status: {fields.get('status', {}).get('name', '')}. {_adf_text(fields.get('description'))}\nComments: {comment_text}"
            )
            if len(text) > 12000:
                jira_truncated = True
                text = text[:12000]
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            evidence.append(EvidenceItem(evidence_id=f"ev-jira-{binding.jira_project_id}-{issue['id']}-{content_hash[:8]}", app="jira", account_ref=binding.jira_project_id, external_id=issue["key"], source_version=fields.get("updated", content_hash), occurred_at=fields.get("updated"), retrieved_at=utc_now(), identity={"project_key": fields.get("project", {}).get("key"), "project_ref": binding.project_ref}, content_text=text, locator={"issue_key": issue["key"]}, metadata={"evidence_type": "delivery_record"}, content_hash=content_hash, agent_generated=any(str(label).startswith("cleardue-op-") for label in fields.get("labels", []))))
        now = utc_now()
        manifests = [
            SourceManifest(app="gmail", account_ref=binding.mailbox_ref, fetched_at=now, status="INCOMPLETE" if truncated else "COMPLETE", scope_description=f"Project reference {binding.project_ref}, cap 30 messages; 12000 characters per snapshot", complete=not truncated, truncated=truncated, source_object_ids=[x["id"] for x in message_ids]),
            SourceManifest(app="jira", account_ref=binding.jira_project_id, fetched_at=now, status="INCOMPLETE" if jira_truncated or not binding.bound_issue_ids else "COMPLETE", scope_description="Bound issues; maximum 100 comments per issue", complete=not jira_truncated and bool(binding.bound_issue_ids), truncated=jira_truncated, source_object_ids=binding.bound_issue_ids),
            SourceManifest(app="razorpay", account_ref=binding.payment_account_ref, fetched_at=now, status="COMPLETE", scope_description="Bound invoice", complete=True, truncated=False, source_object_ids=[binding.invoice_id]),
        ]
        if only is None and not evidence:
            raise ValueError(
                "bounded live evidence is empty; configure DEMO_PROJECT_REF, "
                "DEMO_APPROVER_EMAIL, and DEMO_JIRA_ISSUE_IDS for existing live evidence"
            )
        return financial, evidence, [m for m in manifests if only is None or m.app == only]
