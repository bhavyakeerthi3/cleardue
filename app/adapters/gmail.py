from __future__ import annotations

import base64
import hashlib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from ..models import ReconcileOutcome, Verification, VerificationStatus, WriteOutcome, utc_now
from .transport import FailureKind, ProviderError, request


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


class GmailAdapter:
    base_url = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, token_file: Path, mailbox_ref: str = "me") -> None:
        self.token_file = token_file
        self.mailbox_ref = mailbox_ref

    def _credentials(self) -> Credentials:
        if not self.token_file.exists():
            raise ProviderError(FailureKind.AUTH, "gmail", "OAuth token file is missing; run scripts/preflight.py --authorize-gmail")
        credentials = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self.token_file.write_text(credentials.to_json(), encoding="utf-8")
        if not credentials.valid:
            raise ProviderError(FailureKind.AUTH, "gmail", "OAuth credentials are invalid or expired")
        return credentials

    def _client(self) -> httpx.Client:
        credentials = self._credentials()
        return httpx.Client(
            headers={"Authorization": f"Bearer {credentials.token}"},
            timeout=httpx.Timeout(15, connect=5),
        )

    def profile(self) -> dict[str, Any]:
        with self._client() as client:
            return request(client, "GET", f"{self.base_url}/profile", provider="gmail").json()

    def search_messages(self, query: str, cap: int = 100) -> tuple[list[dict[str, Any]], bool]:
        found: list[dict[str, Any]] = []
        token: str | None = None
        with self._client() as client:
            while len(found) < cap:
                params: dict[str, Any] = {"q": query, "maxResults": min(100, cap - len(found))}
                if token:
                    params["pageToken"] = token
                data = request(client, "GET", f"{self.base_url}/messages", provider="gmail", params=params).json()
                found.extend(data.get("messages", []))
                token = data.get("nextPageToken")
                if not token:
                    return found, False
        return found, token is not None

    def get_message(self, message_id: str) -> dict[str, Any]:
        with self._client() as client:
            return request(
                client, "GET", f"{self.base_url}/messages/{message_id}", provider="gmail", params={"format": "full"}
            ).json()

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        with self._client() as client:
            return request(
                client, "GET", f"{self.base_url}/threads/{thread_id}", provider="gmail", params={"format": "full"}
            ).json()

    def create_draft(self, payload: dict[str, Any], operation_ref: str) -> WriteOutcome:
        message = EmailMessage()
        message["To"] = payload["to"]
        message["Subject"] = payload["subject"]
        message["Message-ID"] = f"<cleardue-{operation_ref}@local.invalid>"
        message["X-ClearDue-Operation"] = operation_ref
        message.set_content(payload["body"])
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        with self._client() as client:
            response = request(
                client, "POST", f"{self.base_url}/drafts", provider="gmail", write=True,
                json={"message": {"raw": raw}},
            )
        data = response.json()
        return WriteOutcome(disposition="ACKNOWLEDGED", external_id=data["id"], provider_request_id=response.headers.get("x-request-id"))

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        with self._client() as client:
            return request(
                client, "GET", f"{self.base_url}/drafts/{draft_id}", provider="gmail", params={"format": "raw"}
            ).json()

    @staticmethod
    def _decode_raw(raw: str) -> str:
        padding = "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(raw + padding).decode("utf-8", errors="replace")

    def find_draft_by_operation(self, operation_ref: str, cap: int = 100) -> ReconcileOutcome:
        candidates: list[str] = []
        token: str | None = None
        with self._client() as client:
            while len(candidates) < cap:
                params: dict[str, Any] = {"q": f'is:draft "{operation_ref}"', "maxResults": min(100, cap - len(candidates))}
                if token:
                    params["pageToken"] = token
                data = request(client, "GET", f"{self.base_url}/drafts", provider="gmail", params=params).json()
                for item in data.get("drafts", []):
                    draft = request(client, "GET", f"{self.base_url}/drafts/{item['id']}", provider="gmail", params={"format": "raw"}).json()
                    raw_text = self._decode_raw(draft["message"]["raw"])
                    if operation_ref in raw_text:
                        candidates.append(item["id"])
                token = data.get("nextPageToken")
                if not token:
                    break
        disposition = "FOUND_MATCH" if len(candidates) == 1 else "MULTIPLE_MATCHES" if len(candidates) > 1 else "NOT_YET_FOUND"
        return ReconcileOutcome(disposition=disposition, candidate_ids=candidates, verified_fields={"operation_ref": operation_ref}, checked_at=utc_now())

    def verify_draft(self, draft_id: str, payload: dict[str, Any], operation_ref: str) -> Verification:
        draft = self.get_draft(draft_id)
        text = self._decode_raw(draft["message"]["raw"])
        expected = [payload["to"], payload["subject"], payload["body"], operation_ref]
        ok = all(value in text for value in expected)
        expected_hash = hashlib.sha256("\n".join(expected).encode()).hexdigest()
        return Verification(
            status=VerificationStatus.VERIFIED if ok else VerificationStatus.MISMATCH,
            observed_external_id=draft_id,
            expected_fields_hash=expected_hash,
            observed_fields={"contains_expected": ok},
            evidence_of_readback={"draft_id": draft_id, "message_id": draft["message"]["id"]},
            checked_at=utc_now(),
        )

