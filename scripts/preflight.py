from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings


def authorize_gmail(settings) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from app.adapters.gmail import SCOPES
    if not settings.google_client_secret_file.exists():
        raise SystemExit(f"Place your OAuth desktop client file at {settings.google_client_secret_file}")
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_client_secret_file), SCOPES)
    credentials = flow.run_local_server(port=0)
    settings.google_token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.google_token_file.write_text(credentials.to_json(), encoding="utf-8")
    print(f"Stored Gmail OAuth token locally at {settings.google_token_file}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-gmail", action="store_true")
    parser.add_argument("--write-checks", action="store_true", help="Create real test objects; disabled by default")
    args = parser.parse_args()
    settings = get_settings()
    if args.authorize_gmail:
        authorize_gmail(settings)
    if settings.mode != "live":
        print(json.dumps({"mode": settings.mode, "live_checks": "skipped", "missing": settings.live_missing()}, indent=2))
        return
    missing = settings.live_missing()
    if missing:
        raise SystemExit("Missing live configuration: " + ", ".join(missing))
    from app.adapters.gmail import GmailAdapter
    from app.adapters.jira import JiraAdapter
    from app.adapters.razorpay import RazorpayAdapter
    gmail = GmailAdapter(settings.google_token_file, settings.gmail_mailbox_ref)
    jira = JiraAdapter(settings.jira_base_url or "", settings.jira_email or "", settings.jira_api_token or "", settings.jira_project_key, settings.jira_issue_type)
    razor = RazorpayAdapter(settings.razorpay_key_id or "", settings.razorpay_key_secret or "", settings.razorpay_account_ref)
    invoice = razor.get_invoice(settings.demo_invoice_id or "")
    result = {"mode": "live", "gmail": gmail.profile().get("emailAddress"), "jira": jira.myself().get("displayName"), "jira_project": settings.jira_project_key, "razorpay_invoice": invoice.invoice_id, "razorpay_status": invoice.provider_status, "write_checks": "not run"}
    if args.write_checks:
        result["write_checks"] = "Use scripts/run_live_checks.py after seeding; it requires explicit --execute"
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
