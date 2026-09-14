from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

from .config import ROOT
from .models import FinancialSnapshot, ReconcileOutcome, Verification, VerificationStatus, WriteOutcome, utc_now


class FixtureProviderSuite:
    """Persistent local simulator for development/evals. It is always labeled fixture."""

    def __init__(self, state_path: Path | None = None) -> None:
        self.source_path = ROOT / "evals" / "fixtures" / "main_case.json"
        self.state_path = state_path or ROOT / "data" / "fixture-provider.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self.reset()

    def reset(self) -> None:
        fixture = json.loads(self.source_path.read_text(encoding="utf-8"))
        state = {"financial": fixture["financial"], "drafts": {}, "issues": {}, "mutation_count": 0}
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _load(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def collect(self) -> tuple[FinancialSnapshot, list[dict[str, Any]], list[dict[str, Any]]]:
        fixture = json.loads(self.source_path.read_text(encoding="utf-8"))
        state = self._load()
        financial = {**state["financial"], "fetched_at": utc_now()}
        evidence = copy.deepcopy(fixture["evidence"]) + state.get("incoming_evidence", [])
        manifests = copy.deepcopy(fixture["manifests"])
        for manifest in manifests:
            manifest["fetched_at"] = utc_now()
            manifest["source_object_ids"] = [e["external_id"] for e in evidence if e["app"] == manifest["app"]]
        return FinancialSnapshot.model_validate(financial), evidence, manifests

    def ingest_fixture_evidence(self, item: dict[str, Any]) -> bool:
        """Local simulator only. This never represents a live mailbox arrival."""
        state = self._load()
        items = state.setdefault("incoming_evidence", [])
        if any(e["external_id"] == item["external_id"] and e["content_hash"] == item["content_hash"] for e in items):
            return False
        items.append(item)
        self._save(state)
        return True

    def create_gmail_draft(self, payload: dict[str, Any], operation_ref: str) -> WriteOutcome:
        state = self._load()
        existing = [key for key, value in state["drafts"].items() if value["operation_ref"] == operation_ref]
        if existing:
            return WriteOutcome(disposition="ACKNOWLEDGED", external_id=existing[0])
        draft_id = f"fixture-draft-{uuid.uuid4().hex[:8]}"
        state["drafts"][draft_id] = {"payload": payload, "operation_ref": operation_ref}
        state["mutation_count"] += 1
        self._save(state)
        return WriteOutcome(disposition="ACKNOWLEDGED", external_id=draft_id)

    def verify_gmail_draft(self, draft_id: str, payload: dict[str, Any], operation_ref: str) -> Verification:
        record = self._load()["drafts"].get(draft_id)
        ok = bool(record and record == {"payload": payload, "operation_ref": operation_ref})
        return Verification(status=VerificationStatus.VERIFIED if ok else VerificationStatus.MISMATCH, observed_external_id=draft_id, expected_fields_hash=operation_ref, observed_fields=record or {}, evidence_of_readback={"fixture": True}, checked_at=utc_now())

    def create_jira_issue(self, payload: dict[str, Any], operation_ref: str, lose_response: bool = False) -> WriteOutcome:
        state = self._load()
        existing = [key for key, value in state["issues"].items() if value["operation_ref"] == operation_ref]
        if existing:
            return WriteOutcome(disposition="ACKNOWLEDGED", external_id=existing[0])
        issue_id = f"CD-{900 + len(state['issues']) + 1}"
        state["issues"][issue_id] = {"payload": payload, "operation_ref": operation_ref}
        state["mutation_count"] += 1
        self._save(state)
        if lose_response:
            return WriteOutcome(disposition="UNCERTAIN", error="injected response loss after fixture write")
        return WriteOutcome(disposition="ACKNOWLEDGED", external_id=issue_id)

    def reconcile_jira(self, operation_ref: str, payload: dict[str, Any]) -> ReconcileOutcome:
        state = self._load()
        ids = [key for key, value in state["issues"].items() if value["operation_ref"] == operation_ref and value["payload"] == payload]
        disposition = "FOUND_MATCH" if len(ids) == 1 else "MULTIPLE_MATCHES" if len(ids) > 1 else "NOT_YET_FOUND"
        return ReconcileOutcome(disposition=disposition, candidate_ids=ids, verified_fields={"operation_ref": operation_ref}, checked_at=utc_now())

    def verify_jira(self, issue_id: str, payload: dict[str, Any], operation_ref: str) -> Verification:
        record = self._load()["issues"].get(issue_id)
        ok = bool(record and record == {"payload": payload, "operation_ref": operation_ref})
        return Verification(status=VerificationStatus.VERIFIED if ok else VerificationStatus.MISMATCH, observed_external_id=issue_id, expected_fields_hash=operation_ref, observed_fields=record or {}, evidence_of_readback={"fixture": True}, checked_at=utc_now())

    def update_razorpay_notes(self, invoice_id: str, desired: dict[str, str], expected_revision: int) -> WriteOutcome:
        state = self._load()
        notes = state["financial"].setdefault("notes", {})
        current_revision = int(notes.get("cleardue_revision", 0))
        if current_revision > expected_revision:
            return WriteOutcome(disposition="DEFINITELY_REJECTED", error="newer revision exists")
        if not all(notes.get(k) == v for k, v in desired.items()):
            state["financial"]["notes"] = {**notes, **desired}
            state["mutation_count"] += 1
            self._save(state)
        return WriteOutcome(disposition="ACKNOWLEDGED", external_id=invoice_id)

    def verify_razorpay_notes(self, invoice_id: str, desired: dict[str, str]) -> Verification:
        state = self._load()
        notes = state["financial"].get("notes", {})
        ok = all(notes.get(key) == value for key, value in desired.items())
        return Verification(status=VerificationStatus.VERIFIED if ok else VerificationStatus.MISMATCH, observed_external_id=invoice_id, expected_fields_hash=str(sorted(desired.items())), observed_fields={key: notes.get(key) for key in desired}, evidence_of_readback={"fixture": True}, checked_at=utc_now())

    def counts(self) -> dict[str, int]:
        state = self._load()
        return {"drafts": len(state["drafts"]), "issues": len(state["issues"]), "mutations": state["mutation_count"]}
