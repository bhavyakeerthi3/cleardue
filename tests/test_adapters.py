from __future__ import annotations

from app.adapters.jira import JiraAdapter
from app.adapters.razorpay import ALLOWED_NOTE_KEYS
from app.fixtures import FixtureProviderSuite
from unittest.mock import Mock
import pytest


def test_jira_operation_label_is_bounded_and_safe():
    label = JiraAdapter.operation_label("ABC / Weird_Value!!" * 10)
    assert label.startswith("cleardue-op-")
    assert len(label) <= len("cleardue-op-") + 48
    assert all(c.isalnum() or c == "-" for c in label)


def test_razorpay_note_allowlist_is_exact():
    assert ALLOWED_NOTE_KEYS == {"cleardue_case", "cleardue_state", "cleardue_revision"}


def test_fixture_razorpay_preserves_unrelated_notes(tmp_path):
    suite = FixtureProviderSuite(tmp_path / "provider.json")
    suite.update_razorpay_notes("inv_cleardue_fixture_001", {"cleardue_case": "case", "cleardue_state": "BLOCKED", "cleardue_revision": "1"}, 1)
    financial, _, _ = suite.collect()
    assert financial.notes["sales_region"] == "south"


@pytest.mark.parametrize("actual", ["approved", "empty", "changed"])
def test_jira_readback_requires_approved_description(actual):
    adapter = JiraAdapter("https://example.atlassian.net", "test@example.com", "test", "KAN", "Task")
    payload = {"summary": "Correct migration mapping", "description": "Correct failed group mapping and obtain new acceptance."}
    description = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": payload["description"] + "\nClearDue operation: op-1"}]}]}
    if actual == "empty": description = None
    if actual == "changed": description["content"][0]["content"][0]["text"] = "Unapproved replacement"
    adapter.get_issue = Mock(return_value={"id": "1", "key": "KAN-10", "fields": {"summary": payload["summary"], "description": description, "labels": [adapter.operation_label("op-1")], "project": {"key": "KAN"}}})
    result = adapter.verify_issue("KAN-10", payload, "op-1")
    assert result.status == ("VERIFIED" if actual == "approved" else "MISMATCH")
