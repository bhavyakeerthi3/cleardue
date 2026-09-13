from __future__ import annotations

from app.adapters.jira import JiraAdapter
from app.adapters.razorpay import ALLOWED_NOTE_KEYS
from app.fixtures import FixtureProviderSuite


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

