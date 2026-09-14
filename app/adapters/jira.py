from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import quote

import httpx

from ..models import ReconcileOutcome, Verification, VerificationStatus, WriteOutcome, utc_now
from .transport import FAULTS, request


class JiraAdapter:
    def __init__(self, base_url: str, email: str, api_token: str, project_key: str, issue_type: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.issue_type = issue_type
        self.auth = (email, api_token)

    def _client(self) -> httpx.Client:
        return httpx.Client(auth=self.auth, headers={"Accept": "application/json"}, timeout=httpx.Timeout(15, connect=5))

    def myself(self) -> dict[str, Any]:
        with self._client() as client:
            return request(client, "GET", f"{self.base_url}/rest/api/3/myself", provider="jira").json()

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        with self._client() as client:
            return request(
                client, "GET", f"{self.base_url}/rest/api/3/issue/{quote(issue_id)}", provider="jira",
                params={"fields": "summary,status,description,labels,updated,project"},
            ).json()

    def get_comments(self, issue_id: str) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        start = 0
        with self._client() as client:
            while True:
                data = request(
                    client, "GET", f"{self.base_url}/rest/api/3/issue/{quote(issue_id)}/comment",
                    provider="jira", params={"startAt": start, "maxResults": 100},
                ).json()
                comments.extend(data.get("comments", []))
                if len(comments) >= 100:
                    return comments[:100]
                start += len(data.get("comments", []))
                if start >= data.get("total", 0):
                    return comments
                if not data.get("comments"):
                    raise ValueError("Jira comment pagination returned an incomplete page")

    def search(self, jql: str, fields: list[str] | None = None, max_results: int = 50) -> list[dict[str, Any]]:
        body = {"jql": jql, "maxResults": max_results, "fields": fields or ["summary", "status", "labels", "updated"]}
        with self._client() as client:
            data = request(client, "POST", f"{self.base_url}/rest/api/3/search/jql", provider="jira", json=body).json()
        return data.get("issues", [])

    def create_metadata(self) -> dict[str, Any]:
        with self._client() as client:
            return request(
                client, "GET", f"{self.base_url}/rest/api/3/issue/createmeta",
                provider="jira", params={"projectKeys": self.project_key, "issuetypeNames": self.issue_type, "expand": "projects.issuetypes.fields"},
            ).json()

    @staticmethod
    def operation_label(operation_ref: str) -> str:
        safe = "".join(ch for ch in operation_ref.lower() if ch.isalnum() or ch == "-")
        return f"cleardue-op-{safe[:48]}"

    def create_remediation(self, payload: dict[str, Any], operation_ref: str) -> WriteOutcome:
        label = self.operation_label(operation_ref)
        description = {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"{payload['description']}\nClearDue operation: {operation_ref}"}]}],
        }
        body = {"fields": {"project": {"key": self.project_key}, "issuetype": {"name": self.issue_type}, "summary": payload["summary"], "description": description, "labels": [label]}}
        with self._client() as client:
            response = request(
                client, "POST", f"{self.base_url}/rest/api/3/issue", provider="jira", write=True, json=body,
                fault_after_success=lambda: FAULTS.consume("jira", "lose_create_response"),
            )
        data = response.json()
        return WriteOutcome(
            disposition="ACKNOWLEDGED", external_id=data["key"],
            external_url=f"{self.base_url}/browse/{data['key']}", provider_request_id=response.headers.get("x-arequestid"),
        )

    def find_issue_by_operation(self, operation_ref: str, expected_summary: str) -> ReconcileOutcome:
        label = self.operation_label(operation_ref)
        issues = self.search(f'project = "{self.project_key}" AND labels = "{label}"', ["summary", "labels", "project"])
        matches = [i for i in issues if i.get("fields", {}).get("summary") == expected_summary and i.get("fields", {}).get("project", {}).get("key") == self.project_key]
        disposition = "FOUND_MATCH" if len(matches) == 1 else "MULTIPLE_MATCHES" if len(matches) > 1 else "FOUND_MISMATCH" if issues else "NOT_YET_FOUND"
        return ReconcileOutcome(disposition=disposition, candidate_ids=[i["key"] for i in matches or issues], verified_fields={"label": label, "summary": expected_summary}, checked_at=utc_now())

    def verify_issue(self, issue_id: str, payload: dict[str, Any], operation_ref: str) -> Verification:
        issue = self.get_issue(issue_id)
        fields = issue["fields"]
        label = self.operation_label(operation_ref)
        expected_description = {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"{payload['description']}\nClearDue operation: {operation_ref}"}]}],
        }
        description_matches = fields.get("description") == expected_description
        ok = issue.get("key") == issue_id and description_matches and fields.get("summary") == payload["summary"] and label in fields.get("labels", []) and fields.get("project", {}).get("key") == self.project_key
        expected_hash = hashlib.sha256(f"{payload['summary']}|{payload['description']}|{operation_ref}|{label}|{self.project_key}".encode()).hexdigest()
        return Verification(
            status=VerificationStatus.VERIFIED if ok else VerificationStatus.MISMATCH,
            observed_external_id=issue.get("key"), expected_fields_hash=expected_hash,
            observed_fields={"summary": fields.get("summary"), "description": fields.get("description"), "description_matches": description_matches, "labels": fields.get("labels"), "project": fields.get("project", {}).get("key")},
            evidence_of_readback={"issue_id": issue.get("id"), "issue_key": issue.get("key")}, checked_at=utc_now(),
        )
