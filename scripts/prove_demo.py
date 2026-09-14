"""One isolated, executable business proof. Fixture-only; never reads local credentials."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.db import Database
from app.demo_data import arrival, prepare_fixture
from app.workflow import WorkflowEngine


def prove(directory):
    settings = Settings(_env_file=None, CLEARDUE_MODE="fixture", CLEARDUE_DB_PATH=directory/"proof.db")
    db = Database(settings.db_path); db.initialize()
    engine = WorkflowEngine(db, settings)
    binding = prepare_fixture(engine)
    event, _ = engine.enqueue(binding.case_id, "INVESTIGATE", "original-proof", {})
    engine.process_next()
    assessment = db.latest_assessment(binding.case_id)
    assert assessment["validation"]["valid"]
    assert db.get_case(binding.case_id)["readiness"] == "BLOCKED"
    before = engine.fixture.counts()
    engine.executor.lose_next_fixture_jira_response = True
    engine.approve_and_execute(binding.case_id, assessment["id"], assessment["plan_hash"], "fixture-proof")
    effects = db.list_actions(binding.case_id)
    assert len(effects) == 2 and all(e["verification_status"] == "VERIFIED" for e in effects)
    recovery = any(a.get("stage") == "RECONCILE" and a.get("outcome") == "FOUND_MATCH" for e in effects for a in e["attempts"])
    assert recovery
    written = engine.fixture.counts()
    engine.investigate(binding.case_id)
    again = db.latest_assessment(binding.case_id)
    engine.approve_and_execute(binding.case_id, again["id"], again["plan_hash"], "fixture-proof")
    assert engine.fixture.counts() == written
    transitions = ["BLOCKED"]
    for condition_id in ("migration-acceptance", "training-change-order"):
        engine.fixture.ingest_fixture_evidence(arrival(binding, condition_id))
        assert engine.poll_evidence(binding.case_id)[0]
        engine.process_next()
        transitions.append(db.get_case(binding.case_id)["readiness"])
    assert transitions == ["BLOCKED", "BLOCKED", "READY_FOR_PAYMENT"]
    ready = db.get_case(binding.case_id)
    assert ready["financial"]["amount_minor"] == binding.expected_amount_minor
    assert ready["financial"]["provider_status"] == "issued"
    repeated, duplicate = engine.enqueue(binding.case_id, "INVESTIGATE", "original-proof", {})
    assert duplicate and repeated == event and not engine.process_next()
    after_replay = engine.fixture.counts()
    assert after_replay == written and len(db.list_actions(binding.case_id)) == 2
    engine.fixture.ingest_fixture_evidence(arrival(binding, "migration-acceptance", sender="unauthorized@example.com"))
    engine.poll_evidence(binding.case_id); engine.process_next()
    rejected = db.latest_assessment(binding.case_id)
    assert not rejected["validation"]["valid"] and not rejected["plan"]["actions"]
    assert db.get_case(binding.case_id)["workflow_status"] == "NEEDS_OPERATOR"
    return {
        "proof_mode": "fixture-only; no live provider or model claims",
        "readiness_transitions": transitions,
        "counts_before_execution": before, "counts_after_execution": written,
        "counts_after_replay": after_replay, "duplicate": duplicate,
        "additional_replay_mutations": after_replay["mutations"]-written["mutations"],
        "response_loss_reconciled": recovery,
        "unauthorized_acceptance": "REJECTED",
        "amount_minor": ready["financial"]["amount_minor"],
        "payment_status": ready["financial"]["provider_status"],
        "verified_effects": [{"app": e["app"], "effect_key": e["effect_key"], "verification": e["verification_status"]} for e in effects],
    }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="cleardue-proof-") as temporary:
        print(json.dumps(prove(Path(temporary)), indent=2))
