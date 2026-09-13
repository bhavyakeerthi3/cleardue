from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import CaseBinding, Readiness, WorkflowStatus, utc_now


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connection() as conn:
            conn.executescript(schema)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_case(self, binding: CaseBinding) -> str:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO cases
                (id, invoice_id, payment_account_ref, customer_id, binding_json,
                 binding_version, workflow_status, readiness, reason_codes_json,
                 version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    binding.case_id,
                    binding.invoice_id,
                    binding.payment_account_ref,
                    binding.customer_id,
                    canonical_json(binding.model_dump(mode="json")),
                    binding.binding_version,
                    WorkflowStatus.IDLE,
                    Readiness.UNKNOWN,
                    "[]",
                    now,
                    now,
                ),
            )
        return binding.case_id

    def rebind_case(self, binding: CaseBinding) -> str:
        """Replace a demo case binding and discard state tied to its previous identity."""
        existing = self.get_case(binding.case_id)
        if not existing:
            return self.create_case(binding)
        existing_identity = {
            key: value for key, value in existing["binding"].items() if key != "binding_version"
        }
        requested_identity = {
            key: value for key, value in binding.model_dump(mode="json").items()
            if key != "binding_version"
        }
        if existing_identity == requested_identity:
            return binding.case_id

        binding = binding.model_copy(
            update={"binding_version": existing["binding_version"] + 1}
        )
        now = utc_now()
        with self.transaction() as conn:
            conn.execute("DELETE FROM actions WHERE case_id=?", (binding.case_id,))
            conn.execute("DELETE FROM assessments WHERE case_id=?", (binding.case_id,))
            conn.execute("DELETE FROM events WHERE case_id=?", (binding.case_id,))
            conn.execute("DELETE FROM evidence WHERE case_id=?", (binding.case_id,))
            conn.execute(
                """UPDATE cases SET invoice_id=?, payment_account_ref=?, customer_id=?,
                binding_json=?, binding_version=?, workflow_status=?, readiness=?,
                reason_codes_json='[]', financial_json=NULL, financial_checked_at=NULL,
                current_assessment_id=NULL, updated_at=?, version=version+1 WHERE id=?""",
                (
                    binding.invoice_id,
                    binding.payment_account_ref,
                    binding.customer_id,
                    canonical_json(binding.model_dump(mode="json")),
                    binding.binding_version,
                    WorkflowStatus.IDLE,
                    Readiness.UNKNOWN,
                    now,
                    binding.case_id,
                ),
            )
        return binding.case_id

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        return self._decode(row) if row else None

    def list_case_evidence(self, case_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence WHERE case_id=? ORDER BY retrieved_at, id", (case_id,)
            ).fetchall()
        return [self._decode(row) for row in rows]

    def latest_assessment(self, case_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM assessments WHERE case_id=? ORDER BY revision DESC LIMIT 1", (case_id,)
            ).fetchone()
        return self._decode(row) if row else None

    def list_actions(self, case_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM actions WHERE case_id=? ORDER BY created_at, id", (case_id,)
            ).fetchall()
        return [self._decode(row) for row in rows]

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        return self._decode(row) if row else None

    def save_evidence(self, case_id: str, item: dict[str, Any]) -> str:
        with self.transaction() as conn:
            existing = conn.execute(
                """SELECT id FROM evidence WHERE case_id=? AND app=? AND account_ref=?
                AND external_id=? AND content_hash=?""",
                (case_id, item["app"], item["account_ref"], item["external_id"], item["content_hash"]),
            ).fetchone()
            if existing:
                return existing["id"]
            conn.execute(
                """INSERT INTO evidence
                (id, case_id, app, account_ref, external_id, source_version, content_hash,
                 occurred_at, retrieved_at, identity_json, content_text, locator_json,
                 metadata_json, supersedes_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item["evidence_id"], case_id, item["app"], item["account_ref"], item["external_id"],
                 item["source_version"], item["content_hash"], item.get("occurred_at"), item["retrieved_at"],
                 canonical_json(item["identity"]), item["content_text"], canonical_json(item["locator"]),
                 canonical_json({**item.get("metadata", {}), "agent_generated": item.get("agent_generated", False)}),
                 item.get("supersedes_id")),
            )
        return item["evidence_id"]

    def save_assessment(self, case_id: str, values: dict[str, Any]) -> str:
        assessment_id = values.get("id") or str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as conn:
            revision = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM assessments WHERE case_id=?", (case_id,)
            ).fetchone()["revision"]
            conn.execute(
                """INSERT INTO assessments
                (id, case_id, revision, input_fingerprint, source_manifest_json,
                 condition_registry_json, decisions_json, claims_json, plan_json, plan_hash,
                 model_id, prompt_version, validation_json, approval_json, usage_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (assessment_id, case_id, revision, values["input_fingerprint"], canonical_json(values["source_manifest"]),
                 canonical_json(values["condition_registry"]), canonical_json(values["decisions"]),
                 canonical_json(values["claims"]), canonical_json(values["plan"]), values["plan_hash"],
                 values["model_id"], values["prompt_version"], canonical_json(values["validation"]),
                 canonical_json(values.get("usage", {})), now),
            )
            conn.execute(
                """UPDATE cases SET current_assessment_id=?, workflow_status='AWAITING_APPROVAL',
                readiness=?, reason_codes_json=?, financial_json=?, financial_checked_at=?,
                updated_at=?, version=version+1 WHERE id=?""",
                (assessment_id, values["readiness"], canonical_json(values["reason_codes"]),
                 canonical_json(values["financial"]), values["financial"]["fetched_at"], now, case_id),
            )
        return assessment_id

    def approve_assessment(self, case_id: str, assessment_id: str, plan_hash: str, approval: dict[str, Any]) -> bool:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT plan_hash FROM assessments WHERE id=? AND case_id=?", (assessment_id, case_id)
            ).fetchone()
            if not row or row["plan_hash"] != plan_hash:
                return False
            if conn.execute("SELECT current_assessment_id FROM cases WHERE id=?", (case_id,)).fetchone()[0] != assessment_id:
                return False
            conn.execute("UPDATE assessments SET approval_json=? WHERE id=?", (canonical_json(approval), assessment_id))
            conn.execute("UPDATE cases SET workflow_status='RUNNING', updated_at=? WHERE id=?", (utc_now(), case_id))
        return True

    def update_case_state(self, case_id: str, workflow_status: str, readiness: str | None = None, reason_codes: list[str] | None = None) -> None:
        assignments = ["workflow_status=?", "updated_at=?", "version=version+1"]
        params: list[Any] = [workflow_status, utc_now()]
        if readiness is not None:
            assignments.append("readiness=?")
            params.append(readiness)
        if reason_codes is not None:
            assignments.append("reason_codes_json=?")
            params.append(canonical_json(reason_codes))
        params.append(case_id)
        with self.transaction() as conn:
            conn.execute(f"UPDATE cases SET {', '.join(assignments)} WHERE id=?", params)

    def list_evaluations(self, suite_run_id: str) -> list[dict[str, Any]]:
        if suite_run_id == "latest":
            with self.connection() as conn:
                row = conn.execute("SELECT suite_run_id FROM eval_results ORDER BY created_at DESC LIMIT 1").fetchone()
            if not row:
                return []
            suite_run_id = row["suite_run_id"]
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM eval_results WHERE suite_run_id=? ORDER BY scenario_id", (suite_run_id,)).fetchall()
        return [self._decode(row) for row in rows]

    def insert_event_once(
        self, case_id: str, source: str, dedupe_key: str, event_type: str, payload: dict[str, Any]
    ) -> tuple[str, bool]:
        event_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM events WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            if existing:
                return existing["id"], True
            conn.execute(
                """INSERT INTO events
                (id, case_id, source, dedupe_key, event_type, payload_json, status,
                 attempt_count, created_at) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', 0, ?)""",
                (event_id, case_id, source, dedupe_key, event_type, canonical_json(payload), now),
            )
            conn.execute(
                "UPDATE cases SET workflow_status='QUEUED', updated_at=?, version=version+1 WHERE id=?",
                (now, case_id),
            )
        return event_id, False

    def reserve_action_once(self, values: dict[str, Any]) -> tuple[str, bool]:
        action_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id, payload_hash FROM actions WHERE effect_key=?", (values["effect_key"],)
            ).fetchone()
            if existing:
                if existing["payload_hash"] != values["payload_hash"]:
                    raise ValueError("effect key reused with a different payload")
                return existing["id"], True
            conn.execute(
                """INSERT INTO actions
                (id, case_id, assessment_id, effect_key, action_type, app, payload_json,
                 payload_hash, preconditions_json, dependencies_json, request_status,
                 verification_status, attempts_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', 'PENDING', '[]', ?, ?)""",
                (
                    action_id, values["case_id"], values["assessment_id"], values["effect_key"],
                    values["action_type"], values["app"], canonical_json(values["payload"]),
                    values["payload_hash"], canonical_json(values.get("preconditions", {})),
                    canonical_json(values.get("dependencies", [])), now, now,
                ),
            )
        return action_id, False

    def transition_action(
        self, action_id: str, expected: str, next_status: str, **updates: Any
    ) -> bool:
        allowed = {"verification_status", "external_id", "external_url", "attempts_json",
                   "verification_json", "next_attempt_at", "last_error_json"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported action columns: {sorted(unknown)}")
        assignments = ["request_status=?", "updated_at=?"]
        params: list[Any] = [next_status, utc_now()]
        for key, value in updates.items():
            assignments.append(f"{key}=?")
            params.append(canonical_json(value) if key.endswith("_json") and value is not None else value)
        params.extend([action_id, expected])
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE actions SET {', '.join(assignments)} WHERE id=? AND request_status=?", params
            )
        return cur.rowcount == 1

    def due_event(self) -> dict[str, Any] | None:
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT * FROM events WHERE status IN ('QUEUED','RETRY_WAIT')
                AND (not_before IS NULL OR not_before<=?) ORDER BY created_at LIMIT 1""", (now,)
            ).fetchone()
            if not row:
                return None
            changed = conn.execute(
                "UPDATE events SET status='RUNNING', attempt_count=attempt_count+1 WHERE id=? AND status=?",
                (row["id"], row["status"]),
            ).rowcount
            if changed != 1:
                return None
            conn.execute(
                "UPDATE cases SET workflow_status='RUNNING', updated_at=? WHERE id=?",
                (now, row["case_id"]),
            )
        result = self._decode(row)
        result["status"] = "RUNNING"
        return result

    def complete_event(self, event_id: str, case_id: str, workflow_status: str = "IDLE") -> None:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE events SET status='COMPLETED', completed_at=? WHERE id=?", (now, event_id)
            )
            conn.execute(
                "UPDATE cases SET workflow_status=?, updated_at=?, version=version+1 WHERE id=?",
                (workflow_status, now, case_id),
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in tuple(result):
            if key.endswith("_json") and result[key] is not None:
                result[key[:-5]] = json.loads(result.pop(key))
        return result
