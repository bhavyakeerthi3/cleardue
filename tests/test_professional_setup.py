import pytest

from app.config import Settings
from app.db import Database
from app.demo_data import prepare_fixture
from app.workflow import WorkflowEngine
from scripts.setup_professional_live import validate_binding


@pytest.mark.parametrize("mismatch", [None, "amount", "project", "lines"])
def test_explicit_binding_matches_real_snapshot_or_rejects(tmp_path, mismatch):
    settings = Settings(_env_file=None, CLEARDUE_MODE="fixture", CLEARDUE_DB_PATH=tmp_path/"setup.db")
    db = Database(settings.db_path); db.initialize()
    engine = WorkflowEngine(db, settings); binding = prepare_fixture(engine)
    financial, evidence, _ = engine._collect(binding)
    settings.demo_case_id = binding.case_id
    settings.demo_invoice_id = binding.invoice_id
    settings.demo_project_ref = binding.project_ref
    settings.jira_project_key = binding.jira_project_id
    settings.gmail_mailbox_ref = binding.mailbox_ref
    settings.razorpay_account_ref = binding.payment_account_ref
    settings.demo_jira_issue_ids = ",".join(binding.bound_issue_ids)
    settings.demo_approver_email = binding.participant_roles[0].email
    if mismatch == "amount": binding.expected_amount_minor += 1
    if mismatch == "project": binding.jira_project_id = "WRONG"
    if mismatch == "lines": binding.reviewed_conditions[0]["line_item_ids"] = ["invented-line"]
    if mismatch:
        with pytest.raises(ValueError): validate_binding(binding, settings, financial, evidence)
    else:
        validate_binding(binding, settings, financial, evidence)
    assert not db.list_actions(binding.case_id)
