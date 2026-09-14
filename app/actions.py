from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from .adapters.gmail import GmailAdapter
from .adapters.jira import JiraAdapter
from .adapters.razorpay import RazorpayAdapter
from .adapters.transport import FailureKind, ProviderError
from .config import Settings
from .db import Database
from .fixtures import FixtureProviderSuite
from .models import ActionType, PlannedAction, VerificationStatus, utc_now
from .policy import effect_key, stable_hash


class ActionExecutor:
    def __init__(self, db: Database, settings: Settings, fixture: FixtureProviderSuite | None = None) -> None:
        self.db = db
        self.settings = settings
        self.fixture = fixture
        self.lose_next_fixture_jira_response = False

    def resume_pending(self) -> bool:
        """Resume only recovery reads; never schedule a fresh PLANNED write."""
        with self.db.connection() as conn:
            rows = conn.execute("SELECT id FROM actions WHERE request_status IN ('IN_FLIGHT','UNCERTAIN','RETRY_WAIT') AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY updated_at LIMIT 8", (utc_now(),)).fetchall()
        for candidate in rows:
            row = self.db.get_action(candidate["id"])
            attempts = list(row.get("attempts", []))
            if sum(a.get("stage") == "RECOVERY_READ" for a in attempts) >= 3:
                self.db.update_case_state(row["case_id"], "NEEDS_OPERATOR")
                continue
            attempts.append({"stage": "RECOVERY_READ", "outcome": "STARTED", "at": utc_now()})
            due = (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()
            if not self.db.transition_action(row["id"], row["request_status"], row["request_status"], attempts_json=attempts, next_attempt_at=due):
                continue
            self.execute(row["id"])
            return True
        return False

    def reserve_plan(self, case: dict[str, Any], assessment: dict[str, Any]) -> list[str]:
        binding = case["binding"]
        action_ids: list[str] = []
        existing_actions = self.db.list_actions(case["id"])
        for action_data in assessment["plan"]["actions"]:
            action = PlannedAction.model_validate(action_data)
            reused_gmail = self._verified_gmail_draft(existing_actions, action, case["binding_version"])
            if reused_gmail:
                action_ids.append(reused_gmail["id"])
                continue
            account_ref = {
                "gmail": binding["mailbox_ref"],
                "jira": binding["jira_project_id"],
                "razorpay": binding["payment_account_ref"],
            }[action.app]
            key = effect_key(action.app, account_ref, case["id"], action)
            # Preserve the first approved operation/payload across paraphrases.
            existing_effect = next((row for row in existing_actions if row["effect_key"] == key), None)
            if existing_effect:
                action_ids.append(existing_effect["id"])
                continue
            payload = {"action": action.model_dump(mode="json"), "operation_ref": key[:24]}
            action_id, _ = self.db.reserve_action_once({
                "case_id": case["id"], "assessment_id": assessment["id"], "effect_key": key,
                "action_type": action.action_type, "app": action.app, "payload": payload,
                "payload_hash": stable_hash(payload),
                "preconditions": {"assessment_id": assessment["id"], "plan_hash": assessment["plan_hash"], "binding_version": case["binding_version"]},
                "dependencies": action.dependencies,
            })
            action_ids.append(action_id)
        return action_ids

    @staticmethod
    def _verified_gmail_draft(
        existing_actions: list[dict[str, Any]], action: PlannedAction, binding_version: int
    ) -> dict[str, Any] | None:
        if action.action_type != ActionType.CREATE_GMAIL_DRAFT:
            return None
        for existing in existing_actions:
            if (
                existing["app"] != "gmail"
                or existing["request_status"] != "ACKNOWLEDGED"
                or existing["verification_status"] != "VERIFIED"
                or not existing.get("external_id")
            ):
                continue
            prior = PlannedAction.model_validate(existing["payload"]["action"])
            if (
                prior.action_type == action.action_type
                and prior.exact_target_id == action.exact_target_id
                and set(action.condition_ids).issubset(prior.condition_ids)
                and existing.get("preconditions", {}).get("binding_version") == binding_version
                and bool(str(prior.payload.get("subject", "")).strip())
                and bool(str(prior.payload.get("body", "")).strip())
            ):
                return existing
        return None

    def execute(self, action_id: str) -> None:
        row = self.db.get_action(action_id)
        if not row or row["request_status"] in {"ACKNOWLEDGED", "CANCELLED"} and row["verification_status"] == "VERIFIED":
            return
        if row["request_status"] == "IN_FLIGHT":
            self._mark_uncertain(row, "worker recovered an in-flight action")
            row = self.db.get_action(action_id)
        if row["request_status"] == "UNCERTAIN":
            self._reconcile(row)
            return
        if row["external_id"] and row["request_status"] in {"ACKNOWLEDGED", "RETRY_WAIT"}:
            # A confirmed remote create needs another read, never another create.
            if row["request_status"] == "RETRY_WAIT":
                if not self.db.transition_action(action_id, "RETRY_WAIT", "ACKNOWLEDGED"):
                    return
                row = self.db.get_action(action_id)
            self._verify(row)
            return
        if row["request_status"] not in {"PLANNED", "RETRY_WAIT"}:
            return
        if not self.db.transition_action(action_id, row["request_status"], "IN_FLIGHT"):
            return
        row = self.db.get_action(action_id)
        action = PlannedAction.model_validate(row["payload"]["action"])
        operation_ref = row["payload"]["operation_ref"]
        try:
            outcome = self._write(action, operation_ref)
        except ProviderError as exc:
            if exc.kind == FailureKind.UNCERTAIN_WRITE:
                self._mark_uncertain(row, str(exc))
                self._reconcile(self.db.get_action(action_id))
            elif exc.kind in {FailureKind.AUTH, FailureKind.PERMISSION, FailureKind.UNSUPPORTED_CAPABILITY}:
                self.db.transition_action(action_id, "IN_FLIGHT", "REJECTED", verification_status="UNKNOWN", last_error_json={"kind": exc.kind, "message": exc.message})
                self.db.update_case_state(row["case_id"], "NEEDS_OPERATOR")
            else:
                self.db.transition_action(action_id, "IN_FLIGHT", "REJECTED", verification_status="UNKNOWN", last_error_json={"kind": exc.kind, "message": exc.message})
            return
        if outcome.disposition == "UNCERTAIN":
            self._mark_uncertain(row, outcome.error or "uncertain provider outcome")
            self._reconcile(self.db.get_action(action_id))
            return
        if outcome.disposition == "DEFINITELY_REJECTED":
            self.db.transition_action(action_id, "IN_FLIGHT", "REJECTED", verification_status="UNKNOWN", last_error_json={"message": outcome.error})
            return
        self.db.transition_action(action_id, "IN_FLIGHT", "ACKNOWLEDGED", external_id=outcome.external_id, external_url=outcome.external_url)
        self._verify(self.db.get_action(action_id))

    def _write(self, action: PlannedAction, operation_ref: str):
        if self.fixture:
            if action.action_type == ActionType.CREATE_GMAIL_DRAFT:
                return self.fixture.create_gmail_draft(action.payload, operation_ref)
            if action.action_type == ActionType.CREATE_JIRA_REMEDIATION:
                lose = self.lose_next_fixture_jira_response
                self.lose_next_fixture_jira_response = False
                return self.fixture.create_jira_issue(action.payload, operation_ref, lose_response=lose)
            if action.action_type == ActionType.UPDATE_RAZORPAY_NOTES:
                return self.fixture.update_razorpay_notes(action.exact_target_id, action.payload, int(action.payload["cleardue_revision"]))
        if action.action_type == ActionType.CREATE_GMAIL_DRAFT:
            return self._gmail().create_draft(action.payload, operation_ref)
        if action.action_type == ActionType.CREATE_JIRA_REMEDIATION:
            if action.exact_target_id != self.settings.jira_project_key:
                raise ProviderError(FailureKind.PERMISSION, "jira", "approved target differs from configured project")
            return self._jira().create_remediation(action.payload, operation_ref)
        if action.action_type == ActionType.UPDATE_RAZORPAY_NOTES:
            return self._razorpay().update_case_notes(action.exact_target_id, action.payload, int(action.payload["cleardue_revision"]))
        raise ProviderError(FailureKind.UNSUPPORTED_CAPABILITY, action.app, "action type is not implemented")

    def _verify(self, row: dict[str, Any]) -> None:
        action = PlannedAction.model_validate(row["payload"]["action"])
        operation_ref = row["payload"]["operation_ref"]
        try:
            if self.fixture:
                if action.app == "gmail":
                    result = self.fixture.verify_gmail_draft(row["external_id"], action.payload, operation_ref)
                elif action.app == "jira":
                    result = self.fixture.verify_jira(row["external_id"], action.payload, operation_ref)
                else:
                    result = self.fixture.verify_razorpay_notes(action.exact_target_id, action.payload)
            elif action.app == "gmail":
                result = self._gmail().verify_draft(row["external_id"], action.payload, operation_ref)
            elif action.app == "jira":
                result = self._jira().verify_issue(row["external_id"], action.payload, operation_ref)
            else:
                result = self._razorpay().verify_case_notes(action.exact_target_id, action.payload)
        except ProviderError as exc:
            self.db.transition_action(row["id"], "ACKNOWLEDGED", "RETRY_WAIT", verification_status="UNKNOWN", last_error_json={"kind": exc.kind, "message": exc.message})
            return
        next_status = "ACKNOWLEDGED" if result.status == VerificationStatus.VERIFIED else "REJECTED"
        self.db.transition_action(row["id"], "ACKNOWLEDGED", next_status, verification_status=result.status, verification_json=result.model_dump(mode="json"))

    def _mark_uncertain(self, row: dict[str, Any], message: str) -> None:
        expected = row["request_status"]
        attempts = list(row.get("attempts", []))
        attempts.append({"stage": "WRITE", "outcome": "UNCERTAIN", "message": message, "at": utc_now()})
        self.db.transition_action(row["id"], expected, "UNCERTAIN", verification_status="UNKNOWN", attempts_json=attempts, last_error_json={"message": message, "at": utc_now()})
        self.db.update_case_state(row["case_id"], "RECOVERING")

    def _reconcile(self, row: dict[str, Any]) -> None:
        action = PlannedAction.model_validate(row["payload"]["action"])
        operation_ref = row["payload"]["operation_ref"]
        try:
            if action.app == "jira":
                result = self.fixture.reconcile_jira(operation_ref, action.payload) if self.fixture else self._jira().find_issue_by_operation(operation_ref, action.payload["summary"])
            elif action.app == "gmail":
                result = self._gmail().find_draft_by_operation(operation_ref) if not self.fixture else None
            else:
                # Razorpay writes update an existing bound object; read-back is the reconciliation.
                verification = self.fixture.verify_razorpay_notes(action.exact_target_id, action.payload) if self.fixture else self._razorpay().verify_case_notes(action.exact_target_id, action.payload)
                if verification.status == VerificationStatus.VERIFIED:
                    self.db.transition_action(row["id"], "UNCERTAIN", "ACKNOWLEDGED", external_id=action.exact_target_id, verification_status="VERIFIED", verification_json=verification.model_dump(mode="json"))
                return
        except ProviderError as exc:
            self.db.transition_action(row["id"], "UNCERTAIN", "UNCERTAIN", last_error_json={"kind": exc.kind, "message": exc.message})
            return
        if result and result.disposition == "FOUND_MATCH":
            external_id = result.candidate_ids[0]
            attempts = list(row.get("attempts", []))
            attempts.append({"stage": "RECONCILE", "outcome": "FOUND_MATCH", "external_id": external_id, "at": utc_now()})
            self.db.transition_action(row["id"], "UNCERTAIN", "ACKNOWLEDGED", external_id=external_id, attempts_json=attempts)
            self._verify(self.db.get_action(row["id"]))
        elif result and result.disposition in {"MULTIPLE_MATCHES", "FOUND_MISMATCH"}:
            self.db.update_case_state(row["case_id"], "NEEDS_OPERATOR")
        # NOT_YET_FOUND deliberately remains UNCERTAIN. No blind retry.

    def _gmail(self) -> GmailAdapter:
        return GmailAdapter(self.settings.google_token_file, self.settings.gmail_mailbox_ref)

    def _jira(self) -> JiraAdapter:
        return JiraAdapter(self.settings.jira_base_url or "", self.settings.jira_email or "", self.settings.jira_api_token or "", self.settings.jira_project_key, self.settings.jira_issue_type)

    def _razorpay(self) -> RazorpayAdapter:
        return RazorpayAdapter(self.settings.razorpay_key_id or "", self.settings.razorpay_key_secret or "", self.settings.razorpay_account_ref)
