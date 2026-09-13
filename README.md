# ClearDue

ClearDue finds what blocks an implementation-services invoice, proposes the smallest supported resolution plan, executes approved operational actions across Gmail, Jira Cloud, and Razorpay Test Mode, and verifies every write by reading it back.

The core demonstration is deliberately difficult: Jira says migration is **Done**, while the contract requires customer acceptance and the customer's acceptance test failed. ClearDue keeps delivery and acceptance separate, creates the minimum coordination work, and remains **BLOCKED** until every reviewed payment condition is satisfied.

## What works now

- One local FastAPI application with SQLite WAL persistence and a durable in-process worker.
- Six frozen tables: cases, evidence, assessments, actions, events, eval_results.
- Real Gmail, Jira Cloud, Razorpay Test Mode adapters with narrow capabilities.
- One structured Gemini reasoning component in live mode using the Google Gen AI SDK.
- Deterministic evidence-reference, identity, money, action and readiness checks.
- Approval bound to the current assessment and exact plan hash.
- Event/effect deduplication, read-back verification and uncertain Jira-create reconciliation.
- Demo fault injection that discards a create response after the issue is written.
- A local fixture mode for development and labeled evaluations. Fixture results are never labeled live.
- Two-page operator UI and 22-scenario deterministic reliability evaluation.

The completed local live run recorded a verified Gmail draft, Jira KAN-4 remediation, and Razorpay Test Mode case note. Credentials and the private live database are excluded from submission. Fixture evaluations are separate from these live results. The historical Gmail draft has an empty subject/body; its verification is not proof of useful message content.

## Launch the preserved local demo

From the existing `cleardue` directory with its configured local `.env`, run only:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Do not reseed, rebind, reset, or rerun live execution on the completed case. Inspect the existing case read-only. The quick start below is for a fresh evaluator checkout, not this preserved live installation.

## Quick start in fixture mode

```powershell
cd cleardue
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe scripts\seed_demo.py
.\.venv\Scripts\python.exe evals\run.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The password is the local value of `CLEARDUE_OPERATOR_PASSWORD`; replace the example before recording.

## Live test setup

Never paste secrets into chat or commit `.env`. Copy `.env.example` to `.env`, set `CLEARDUE_MODE=live`, and fill these values locally:

- `GEMINI_API_KEY` and `GEMINI_MODEL=gemini-3.6-flash`.
- `GOOGLE_CLIENT_SECRET_FILE` pointing to a Google OAuth desktop-client JSON.
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`, `JIRA_ISSUE_TYPE`.
- Razorpay **Test Mode** `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_ACCOUNT_REF`, and `DEMO_INVOICE_ID`.
- `DEMO_APPROVER_EMAIL`, bound to the synthetic customer's authorized approver.
- `DEMO_PROJECT_REF` and comma-separated `DEMO_JIRA_ISSUE_IDS`, bound to existing live evidence.
- New random `CLEARDUE_SESSION_SECRET` and `CLEARDUE_OPERATOR_PASSWORD`.

Then run:

```powershell
.\.venv\Scripts\python.exe scripts\preflight.py --authorize-gmail
.\.venv\Scripts\python.exe scripts\preflight.py
```

For a new live case in a separate database only, `scripts\seed_demo.py` creates its explicit binding. Do not run it against the completed live case: rebinding can remove its history. Consult `docs/live-evidence-setup.md` for evidence preparation. External execution is a separate deliberate operation, not part of preflight or release verification.

`run_live_checks.py --execute` creates an actual Gmail draft and Jira issue and writes ClearDue namespaced notes to the bound Razorpay test invoice. It never sends email, refunds, changes invoice amounts, or changes payment status.

Live investigation expects genuine relevant Gmail evidence containing the configured `DEMO_PROJECT_REF` and the configured participant. Bind existing Jira issue IDs explicitly. The preserved demo uses project KAN and project reference `CLEARDUE-LIVE-2026-09-14`.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app scripts evals tests
node --check app\static\app.js
```

The evaluator persists results and prints a run ID:

```powershell
.\.venv\Scripts\python.exe evals\run.py
```

Open `/evaluations?run_id=<printed-id>`. Results are labeled `fixture-rules`; live integration results must be reported separately.

Release checks passed: 41 pytest tests and 22 fixture-rule assertions. The latter are not 22 live-provider or model-accuracy tests. Scenario 20 is a placeholder; scenario 17 does not inject a crash. Dashboard zero counters are not independently measured error rates. Jira response-loss recovery is fixture-proven, not live fault-proven. The current live business case remains BLOCKED; no payment or live all-satisfied readiness is claimed. Same-event duplicate replay was demonstrated; equivalent new investigations are not covered by that guarantee.

## Safety boundaries

The model receives evidence and returns a strict proposal. It has no provider clients, database mutation access, credentials, or arbitrary tool execution. Provider targets and recipients derive from reviewed case bindings. The runtime exposes no send-email, refund, force-ready, amount-edit, or generic execute route.

See [docs/reliability.md](docs/reliability.md) for failure semantics and [docs/demo-script.md](docs/demo-script.md) for the two-minute sequence.
Use [docs/live-evidence-setup.md](docs/live-evidence-setup.md) to bind existing Gmail and Jira evidence and run a read-only live investigation.
