"""Validate an explicitly reviewed binding against provider reads; create only local state."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.db import Database
from app.models import CaseBinding, ConditionKind
from app.workflow import WorkflowEngine


def validate_binding(binding, settings, financial, evidence):
    expected = {
        "case_id": settings.demo_case_id, "invoice_id": settings.demo_invoice_id,
        "jira_project_id": settings.jira_project_key, "project_ref": settings.demo_project_ref,
        "mailbox_ref": settings.gmail_mailbox_ref, "payment_account_ref": settings.razorpay_account_ref,
    }
    for field, value in expected.items():
        if not value or getattr(binding, field) != value:
            raise ValueError(f"Reviewed binding must match configured {field}")
    if set(binding.bound_issue_ids) != set(settings.demo_jira_issue_id_list) or not 1 <= len(binding.bound_issue_ids) <= 10:
        raise ValueError("Bind 1–10 explicit configured Jira issue IDs")
    if binding.customer_id != financial.customer_id or binding.expected_amount_minor != financial.amount_minor or binding.expected_currency != financial.currency:
        raise ValueError("Reviewed customer, amount and currency must match the actual invoice")
    rules = binding.reviewed_conditions
    ids = [r.get("condition_id") for r in rules]
    if not ids or not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("A nonempty, unique reviewed condition registry is required")
    covered = set()
    for rule in rules:
        for required in ("condition_id", "kind", "line_item_ids", "requirement", "scope", "source_app"):
            if not rule.get(required):
                raise ValueError(f"Reviewed condition missing {required}")
        ConditionKind(rule["kind"])
        if rule["source_app"] not in {"gmail", "jira"}:
            raise ValueError("Reviewed authority source must be gmail or jira")
        if rule["source_app"] == "gmail" and not any(p.email == settings.demo_approver_email and p.role == "authorized_customer_approver" and rule["scope"] in p.scope for p in binding.participant_roles):
            raise ValueError("Gmail condition needs an explicitly configured authorized approver and scope")
        covered.update(rule["line_item_ids"])
    if covered != {line.id for line in financial.lines}:
        raise ValueError("Reviewed conditions must cover exactly the real invoice line IDs")
    from app.policy import validate_identity
    errors = validate_identity(binding, evidence, financial.customer_id)
    if errors:
        raise ValueError("Provider evidence identity does not match the reviewed binding")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", required=True, type=Path, help="Private reviewed CaseBinding JSON; never fixture data")
    args = parser.parse_args()
    settings = Settings()
    if settings.mode != "live":
        raise SystemExit("Set CLEARDUE_MODE=live")
    if settings.db_path.exists() or settings.db_path.resolve() == (ROOT/"data/cleardue.db").resolve():
        raise SystemExit("Use a NEW database path. Existing databases/history are never replaced.")
    missing = settings.live_missing()
    if missing:
        raise SystemExit("Missing local configuration: " + ", ".join(missing))
    binding = CaseBinding.model_validate_json(args.binding.read_text(encoding="utf-8"))
    # Constructing the engine does not initialize or mutate the database.
    db = Database(settings.db_path)
    engine = WorkflowEngine(db, settings)
    financial, evidence, manifests = engine._collect(binding)
    validate_binding(binding, settings, financial, evidence)
    if len(manifests) != 3 or any(not m.complete or m.truncated for m in manifests):
        raise SystemExit("All three sources must be complete before saving the new binding")
    db.initialize(); db.create_case(binding)
    print(json.dumps({"case_id": binding.case_id, "jira_project": binding.jira_project_id, "invoice_id": binding.invoice_id, "reviewed_conditions": len(binding.reviewed_conditions), "external_writes": 0}))


if __name__ == "__main__": main()
