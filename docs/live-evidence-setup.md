# Live evidence setup

ClearDue live mode requires an explicit correlation key, authorized customer sender, and existing Jira delivery records. Setup is manual so normal investigation remains read-only.

## Environment binding

Choose one stable ASCII project reference and use it verbatim in every source:

```dotenv
DEMO_PROJECT_REF=CLEARDUE-LIVE-2026-09-13
DEMO_APPROVER_EMAIL=customer-approver@example.com
DEMO_JIRA_ISSUE_IDS=CD-123
```

- `DEMO_PROJECT_REF` is the exact correlation token present in the Gmail subject or body and Jira summary or description.
- `DEMO_APPROVER_EMAIL` is the exact sender address of the relevant message received by the connected Gmail mailbox. It must represent the authorized customer approver for the demo account.
- `DEMO_JIRA_ISSUE_IDS` is a comma-separated list of existing issue keys in `JIRA_PROJECT_KEY`. One issue is sufficient for the demo.

## Jira delivery record

Create the issue manually in the configured Jira project. Use the project's configured issue type and these fields:

- Summary: `[CLEARDUE-LIVE-2026-09-13] SSO migration delivery`
- Description: `Project reference: CLEARDUE-LIVE-2026-09-13. The SSO migration implementation was deployed. Group mapping is included in the customer acceptance test scope.`
- Status: the real delivery state. Use `Done` only after the synthetic demo deployment has actually been marked complete.
- Project: the exact project configured by `JIRA_PROJECT_KEY`.

Copy the resulting issue key, such as `CD-123`, into `DEMO_JIRA_ISSUE_IDS`. Do not use a key from another project.

## Gmail evidence

The connected mailbox must receive a genuine message from the exact `DEMO_APPROVER_EMAIL` address. The message must contain the exact `DEMO_PROJECT_REF` token. For the payment-blocker demonstration, use a real synthetic acceptance-test record such as:

```text
Subject: [CLEARDUE-LIVE-2026-09-13] Migration acceptance result

Project reference: CLEARDUE-LIVE-2026-09-13.
The SSO migration acceptance test failed because the group mapping did not match the approved test cases.
I do not accept the migration milestone yet. Please correct the mapping and provide a new acceptance test.
```

If the acceptance requirement is not already present in a genuine message within the bounded scope, also provide a separate message from the same authorized sender:

```text
Subject: [CLEARDUE-LIVE-2026-09-13] Migration billing condition

Project reference: CLEARDUE-LIVE-2026-09-13.
The migration milestone is billable only after a successful customer acceptance test and written acceptance from me.
```

These messages are synthetic demo records sent through the real connected mailbox. Their wording must match the scenario actually being demonstrated. ClearDue searches only for the exact project reference and exact sender.

## Read-only verification

After saving the environment values, rebind the case and run the investigation-only script:

```powershell
.\.venv\Scripts\python.exe scripts\preflight.py
.\.venv\Scripts\python.exe scripts\seed_demo.py
.\.venv\Scripts\python.exe scripts\run_live_investigation.py
```

The final command performs provider reads and one Gemini reasoning call. It does not approve the plan, reserve action rows, or execute Gmail, Jira, or Razorpay writes.
