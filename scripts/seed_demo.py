from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ROOT, get_settings
from app.db import Database
from app.models import CaseBinding


def main() -> None:
    settings = get_settings()
    db = Database(settings.db_path)
    db.initialize()
    fixture = json.loads((ROOT / "evals" / "fixtures" / "main_case.json").read_text(encoding="utf-8"))
    if settings.mode == "fixture":
        binding = CaseBinding.model_validate(fixture["binding"])
    else:
        if not settings.demo_invoice_id:
            raise SystemExit("DEMO_INVOICE_ID is required in live mode")
        missing_binding = [
            name for name, present in {
                "DEMO_APPROVER_EMAIL": settings.demo_approver_email,
                "DEMO_PROJECT_REF": settings.demo_project_ref,
                "DEMO_JIRA_ISSUE_IDS": settings.demo_jira_issue_id_list,
                "JIRA_PROJECT_KEY": settings.jira_project_key,
            }.items() if not present
        ]
        if missing_binding:
            raise SystemExit("Missing explicit live binding: " + ", ".join(missing_binding))
        from app.adapters.razorpay import RazorpayAdapter
        invoice = RazorpayAdapter(settings.razorpay_key_id or "", settings.razorpay_key_secret or "", settings.razorpay_account_ref).get_invoice(settings.demo_invoice_id)
        binding = CaseBinding(
            case_id=settings.demo_case_id, payment_account_ref=settings.razorpay_account_ref,
            invoice_id=invoice.invoice_id, customer_id=invoice.customer_id, mailbox_ref=settings.gmail_mailbox_ref,
            participant_roles=[{"email": settings.demo_approver_email, "role": "authorized_customer_approver", "scope": ["implementation", "migration", "training"]}],
            jira_project_id=settings.jira_project_key,
            project_ref=settings.demo_project_ref,
            bound_issue_ids=settings.demo_jira_issue_id_list, contract_evidence_ids=[],
        )
    if settings.mode == "live":
        db.rebind_case(binding)
    else:
        db.create_case(binding)
    print(f"Seeded {binding.case_id} in {settings.mode} mode at {settings.db_path}")


if __name__ == "__main__":
    main()
