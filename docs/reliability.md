# Reliability brief

ClearDue uses three layers of duplicate defense: incoming source-event deduplication, unique business-effect keys, and remote reconciliation after uncertain writes. It persists an intent before writing. A worker restart treats an `IN_FLIGHT` action as uncertain and reconciles it rather than recreating the object.

HTTP success is not enough. Gmail drafts, Jira issues, and Razorpay notes become `VERIFIED` only after a read-back matches their expected recipient/project/invoice and content marker. A lost Jira create response triggers a project-scoped search for `cleardue-op-<hash>`. One exact match is fetched and verified; no match remains uncertain because Jira search may be eventually consistent; multiple or mismatched results require the operator.

Readiness is deterministic. Required sources must be complete, identities must match exact bindings, all reviewed conditions must be satisfied, the invoice must have a current payable provider state, and no unresolved conflict or validation error may remain. The result is timestamped and means conditions are satisfied as of that check. It never means paid.

Email and Jira text are untrusted evidence. The model cannot authorize actions. The only effects are a Gmail draft, Jira remediation issue, and three namespaced Razorpay notes. No send, refund, amount change, cancellation, payment mutation, or generic execution capability exists.

Current validation: automated unit/integration-style tests and the 22-scenario fixture-rules evaluator. Live provider preflight is still required because credentials are intentionally absent from the repository. Fixture results and provider results must remain separately labeled in the submission.

