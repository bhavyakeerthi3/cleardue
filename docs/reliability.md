# Reliability boundaries

Exact immutable evidence spans, reviewed condition IDs, invoice/customer/project identity, authority and freshness are checked outside the model. Invalid proposals cannot reserve actions. Approval requires the current assessment, exact plan hash and fresh matching provider fingerprint within a time window.

The ledger persists intent before a write. Equivalent business operations reuse stable effect keys. Gmail reuse requires useful nonempty content covering the requested conditions; the old empty live draft is not sufficient. A lost create response triggers reconciliation rather than a blind create. Acknowledged IDs are verified by reads. Bounded recovery ends in operator attention if unresolved. Historical rejection remains recorded.

Executed fixture tests cover the full business loop, semantic replay, response loss, process interruption after creation, stale approval, unauthorized/wrong-scope/stale/agent-generated acceptance, retraction, changed money and provider failure. These are not live model quality or provider reliability measurements.

Historical live proof covers provider writes/read-back and same-event replay, not live response loss or completed live readiness. The old Gmail content was empty and is not useful-content proof.

Boundaries: single-process SQLite, conservative English authority checks, bounded searches, periodic polling and a trusted loopback console. Source refresh failure invalidates readiness. Ambiguity needs human review.

Time passing can invalidate acceptance even when the source text is unchanged. The watcher revokes readiness on expiry, and approval rechecks authority before action reservation. Jira read-back requires the approved description and operation reference as well as the correct project, summary and label; unexpected document structure fails closed for review.
