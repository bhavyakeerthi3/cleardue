# Explicit live binding

Use a new case/database; do not replace historical effects. Setup rejects an existing database. Keep the reviewed JSON at ignored `data/professional-binding.json`, with no keys/tokens inside.

The JSON must match `CaseBinding` in `app/models.py`:

| Field | Required reviewed value |
|---|---|
| case_id | New DEMO_CASE_ID |
| binding_version | 1 |
| invoice_id / customer_id | Actual invoice and customer IDs from Razorpay |
| expected_amount_minor / expected_currency | Actual invoice values, never overridden for the demo |
| payment_account_ref / mailbox_ref | Configured provider/account references |
| jira_project_id / project_ref | Existing configured project key and explicit project reference |
| bound_issue_ids | Array of existing delivery issue keys matching DEMO_JIRA_ISSUE_IDS |
| participant_roles | Email, role `authorized_customer_approver`, explicit scope array |
| contract_evidence_ids | Existing evidence IDs, or empty array if not applicable |
| reviewed_conditions | Reviewed rules covering exactly every actual invoice line ID |

Each condition contains `condition_id`, `kind` (e.g. ACCEPTANCE), `line_item_ids`, `requirement`, `scope` (e.g. migration), `source_app` (gmail or jira), and optionally `max_age_seconds` and ISO-8601 `fresh_after`. Conditions are operator-reviewed contractual obligations, not model-invented rules. Gmail conditions require the configured approver with the matching scope.

The bound Jira issue must genuinely describe the project's delivery and observed test problem. Gmail must contain genuine relevant project/milestone evidence; an invoice notification is insufficient. Delivery Done alone cannot establish acceptance.

For the positive transition, the authorized participant must actually review the successful test and send explicit written acceptance identifying the project and milestone. Only if true, wording such as “Project [reference]. I confirm the migration milestone: the acceptance test passed and I give written acceptance” supplies useful evidence. Training approval remains separate where required. Do not fabricate a customer message to force readiness. Fresh denial or withdrawal blocks it.

Run the README preflights, then `scripts/setup_professional_live.py --binding data/professional-binding.json`. This reads providers and validates invoice identity, amount, scope and source completeness before saving only local state. It creates no issues, drafts or notes. Then investigate in the UI and review the exact plan before approving any real write.
