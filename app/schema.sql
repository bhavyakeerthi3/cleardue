PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY,
  invoice_id TEXT NOT NULL,
  payment_account_ref TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
  binding_version INTEGER NOT NULL CHECK(binding_version > 0),
  workflow_status TEXT NOT NULL CHECK(workflow_status IN ('QUEUED','RUNNING','AWAITING_APPROVAL','IDLE','RECOVERING','NEEDS_OPERATOR')),
  readiness TEXT NOT NULL CHECK(readiness IN ('UNKNOWN','BLOCKED','READY_FOR_PAYMENT','CLOSED')),
  reason_codes_json TEXT NOT NULL CHECK(json_valid(reason_codes_json)),
  financial_json TEXT CHECK(financial_json IS NULL OR json_valid(financial_json)),
  financial_checked_at TEXT,
  current_assessment_id TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(payment_account_ref, invoice_id)
);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id),
  app TEXT NOT NULL,
  account_ref TEXT NOT NULL,
  external_id TEXT NOT NULL,
  source_version TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  occurred_at TEXT,
  retrieved_at TEXT NOT NULL,
  identity_json TEXT NOT NULL CHECK(json_valid(identity_json)),
  content_text TEXT NOT NULL,
  locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
  metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
  supersedes_id TEXT REFERENCES evidence(id),
  UNIQUE(case_id, app, account_ref, external_id, content_hash)
);

CREATE TABLE IF NOT EXISTS assessments (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id),
  revision INTEGER NOT NULL,
  input_fingerprint TEXT NOT NULL,
  source_manifest_json TEXT NOT NULL CHECK(json_valid(source_manifest_json)),
  condition_registry_json TEXT NOT NULL CHECK(json_valid(condition_registry_json)),
  decisions_json TEXT NOT NULL CHECK(json_valid(decisions_json)),
  claims_json TEXT NOT NULL CHECK(json_valid(claims_json)),
  plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
  plan_hash TEXT NOT NULL,
  model_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  validation_json TEXT NOT NULL CHECK(json_valid(validation_json)),
  approval_json TEXT CHECK(approval_json IS NULL OR json_valid(approval_json)),
  usage_json TEXT NOT NULL CHECK(json_valid(usage_json)),
  created_at TEXT NOT NULL,
  UNIQUE(case_id, revision)
);

CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id),
  assessment_id TEXT NOT NULL REFERENCES assessments(id),
  effect_key TEXT UNIQUE NOT NULL,
  action_type TEXT NOT NULL,
  app TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  payload_hash TEXT NOT NULL,
  preconditions_json TEXT NOT NULL CHECK(json_valid(preconditions_json)),
  dependencies_json TEXT NOT NULL CHECK(json_valid(dependencies_json)),
  request_status TEXT NOT NULL CHECK(request_status IN ('PLANNED','IN_FLIGHT','ACKNOWLEDGED','UNCERTAIN','RETRY_WAIT','REJECTED','CANCELLED')),
  verification_status TEXT NOT NULL CHECK(verification_status IN ('PENDING','VERIFIED','MISMATCH','UNKNOWN')),
  external_id TEXT,
  external_url TEXT,
  attempts_json TEXT NOT NULL CHECK(json_valid(attempts_json)),
  verification_json TEXT CHECK(verification_json IS NULL OR json_valid(verification_json)),
  next_attempt_at TEXT,
  last_error_json TEXT CHECK(last_error_json IS NULL OR json_valid(last_error_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id),
  source TEXT NOT NULL,
  dedupe_key TEXT UNIQUE NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','COMPLETED','RETRY_WAIT','FAILED')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  not_before TEXT,
  error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
  id TEXT PRIMARY KEY,
  suite_run_id TEXT NOT NULL,
  scenario_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  model_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  expected_json TEXT NOT NULL CHECK(json_valid(expected_json)),
  actual_json TEXT NOT NULL CHECK(json_valid(actual_json)),
  metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
  passed INTEGER NOT NULL CHECK(passed IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE(suite_run_id, scenario_id, mode)
);

CREATE INDEX IF NOT EXISTS idx_events_due ON events(status, not_before);
CREATE INDEX IF NOT EXISTS idx_actions_due ON actions(request_status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence(case_id);
CREATE INDEX IF NOT EXISTS idx_assessments_case_revision ON assessments(case_id, revision DESC);

