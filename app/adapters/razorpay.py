from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from ..models import FinancialSnapshot, LineItem, Verification, VerificationStatus, WriteOutcome, utc_now
from .transport import FailureKind, ProviderError, request


ALLOWED_NOTE_KEYS = {"cleardue_case", "cleardue_state", "cleardue_revision"}


class RazorpayAdapter:
    base_url = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str, account_ref: str) -> None:
        self.auth = (key_id, key_secret)
        self.account_ref = account_ref

    def _client(self) -> httpx.Client:
        return httpx.Client(auth=self.auth, headers={"Accept": "application/json"}, timeout=httpx.Timeout(15, connect=5))

    def get_invoice_raw(self, invoice_id: str) -> dict[str, Any]:
        with self._client() as client:
            return request(client, "GET", f"{self.base_url}/invoices/{invoice_id}", provider="razorpay").json()

    def get_invoice(self, invoice_id: str) -> FinancialSnapshot:
        data = self.get_invoice_raw(invoice_id)
        lines = [LineItem(id=item["id"], description=item.get("name") or item.get("description") or "Line item", amount_minor=int(item["amount"]), quantity=int(item.get("quantity", 1))) for item in data.get("line_items", [])]
        projection = {key: data.get(key) for key in ("id", "customer_id", "currency", "amount", "amount_paid", "amount_due", "status", "line_items")}
        fingerprint = hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return FinancialSnapshot(
            invoice_id=data["id"], customer_id=data["customer_id"], currency=data["currency"],
            amount_minor=int(data["amount"]), amount_paid_minor=int(data.get("amount_paid") or 0),
            amount_due_minor=int(data.get("amount_due") if data.get("amount_due") is not None else data["amount"]),
            provider_status=data["status"], lines=lines, notes={str(k): str(v) for k, v in (data.get("notes") or {}).items()},
            financial_fingerprint=fingerprint, fetched_at=utc_now(),
            raw_accounting={key: data.get(key) for key in ("gross_amount", "tax_amount", "taxable_amount", "partial_payment")},
        )

    def update_case_notes(self, invoice_id: str, desired: dict[str, str], expected_revision: int) -> WriteOutcome:
        if not set(desired).issubset(ALLOWED_NOTE_KEYS):
            raise ProviderError(FailureKind.UNSUPPORTED_CAPABILITY, "razorpay", "only namespaced ClearDue notes are allowed")
        current = self.get_invoice_raw(invoice_id)
        notes = {str(k): str(v) for k, v in (current.get("notes") or {}).items()}
        current_revision = int(notes.get("cleardue_revision", "0")) if notes.get("cleardue_revision", "0").isdigit() else 0
        if current_revision > expected_revision:
            raise ProviderError(FailureKind.DEFINITE_REJECTION, "razorpay", "refusing to overwrite a newer ClearDue revision")
        merged = {**notes, **desired}
        if all(notes.get(key) == value for key, value in desired.items()):
            return WriteOutcome(disposition="ACKNOWLEDGED", external_id=invoice_id)
        with self._client() as client:
            response = request(client, "PATCH", f"{self.base_url}/invoices/{invoice_id}", provider="razorpay", write=True, json={"notes": merged})
        return WriteOutcome(disposition="ACKNOWLEDGED", external_id=invoice_id, provider_request_id=response.headers.get("x-request-id"))

    def verify_case_notes(self, invoice_id: str, desired: dict[str, str]) -> Verification:
        data = self.get_invoice_raw(invoice_id)
        notes = {str(k): str(v) for k, v in (data.get("notes") or {}).items()}
        ok = all(notes.get(key) == value for key, value in desired.items())
        expected_hash = hashlib.sha256(json.dumps(desired, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return Verification(
            status=VerificationStatus.VERIFIED if ok else VerificationStatus.MISMATCH,
            observed_external_id=invoice_id, expected_fields_hash=expected_hash,
            observed_fields={key: notes.get(key) for key in desired}, evidence_of_readback={"invoice_id": invoice_id}, checked_at=utc_now(),
        )

