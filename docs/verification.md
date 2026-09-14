# Measured proof and limits

Verified locally on September 15, 2026:

| Check | Result | What this establishes |
|---|---|---|
| Full pytest suite | 70 passed | Deterministic behavior, provider request/read-back tests, fixture workflow and HTTP smoke |
| Executed evaluation subset | 40 passed, run `proof-69d68ddeba` | Actual fixture/unit assertions, not 40 live-provider experiments |
| `scripts/prove_demo.py` | PASS | Full isolated business loop, response loss, reuse and unauthorized-acceptance rejection |
| Compileall, JS syntax, dependency check | PASS | Local build/import/dependency checks |
| Browser rehearsal | PASS | Real UI → local API → persisted fixture ledger → readiness → replay |
| Browser console | No captured errors | Observed during this rehearsal, not a global guarantee |

## Business proof

The proof command observed zero fixture effects before approval and one Gmail draft plus one Jira issue afterwards. Both verified. The test intentionally discards the Jira create response; reconciliation finds the same issue. A repeated investigation reuses existing effects. Migration acceptance leaves the invoice blocked until training approval arrives. Readiness then becomes READY_FOR_PAYMENT; invoice amount stays 240000000 minor INR units and provider status stays `issued`.

Original-event replay returns `duplicate=true`. Counts remain drafts 1, issues 1, simulator mutations 2 and ledger rows 2. A subsequent unauthorized acceptance causes deterministic rejection with no executable plan. The broader tests additionally check paraphrased purposes, process interruption after creation, changed money, wrong scope, expired acceptance and unavailable sources.

## Historical live evidence

An earlier build has persisted verified Gmail/Razorpay/Jira effects and rejected Jira history. The original database contains six action rows, including KAN-4 and KAN-5. These objects were not recreated or reset during this build. The earlier Gmail draft was empty; its historical verification is not evidence of a useful message.

This release's complete positive loop and lost-response recovery are fixture-proven. A fresh live Gemini investigation and useful live effects under the new logic have not been certified by this rehearsal. Live validation needs a separately reviewed binding and genuine current authorized acceptance. No payment collection is claimed.

## Reproduce

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe evals\run.py
.\.venv\Scripts\python.exe scripts\prove_demo.py
.\.venv\Scripts\python.exe scripts\run_professional.py --new-run --port 8001
```

GitHub Actions runs offline checks on pushes; consult the actual workflow result before calling a remote build successful. No local credentials or private live databases are part of the source submission.
