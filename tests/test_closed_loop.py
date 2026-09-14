import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.config import Settings
from app.db import Database
from app.demo_data import arrival, prepare_fixture
from app.workflow import WorkflowEngine


@pytest.fixture
def loop(tmp_path):
    settings = Settings(_env_file=None, CLEARDUE_MODE="fixture", CLEARDUE_DB_PATH=tmp_path/"loop.db")
    db = Database(settings.db_path); db.initialize()
    engine = WorkflowEngine(db, settings)
    binding = prepare_fixture(engine)
    return engine, binding


def investigate(engine, binding):
    engine.investigate(binding.case_id)
    return engine.db.latest_assessment(binding.case_id)


def test_closed_loop_new_evidence_and_semantic_replay(loop):
    engine, binding = loop
    event, _ = engine.enqueue(binding.case_id, "INVESTIGATE", "original", {})
    engine.process_next()
    assessment = engine.db.latest_assessment(binding.case_id)
    assert assessment["validation"]["valid"], assessment["validation"]
    assert engine.db.get_case(binding.case_id)["readiness"] == "BLOCKED"
    engine.executor._write = Mock(wraps=engine.executor._write)
    engine.approve_and_execute(binding.case_id, assessment["id"], assessment["plan_hash"], "test")
    before = engine.fixture.counts()
    for row in engine.db.list_actions(binding.case_id):
        assert row["verification_status"] == "VERIFIED"
        if row["app"] == "gmail":
            assert row["payload"]["action"]["payload"]["subject"].strip()
            assert binding.project_ref in row["payload"]["action"]["payload"]["body"]
    # A new investigation with different wording must use the original business effects.
    original_reason = engine.reasoner.reason
    def paraphrase(bundle):
        proposal, usage = original_reason(bundle)
        for a in proposal.action_intents:
            a.purpose = "Please " + a.purpose
            a.content_fields["summary"] = "Equivalent alternate phrasing"
        return proposal, usage
    engine.reasoner.reason = paraphrase
    second = investigate(engine, binding)
    calls = engine.executor._write.call_count
    engine.approve_and_execute(binding.case_id, second["id"], second["plan_hash"], "test")
    assert engine.executor._write.call_count == calls
    assert engine.fixture.counts() == before
    engine.reasoner.reason = original_reason
    engine.fixture.ingest_fixture_evidence(arrival(binding, "migration-acceptance"))
    assert engine.poll_evidence(binding.case_id)[0]
    engine.process_next()
    assert engine.db.get_case(binding.case_id)["readiness"] == "BLOCKED" # training still missing
    engine.fixture.ingest_fixture_evidence(arrival(binding, "training-change-order"))
    assert engine.poll_evidence(binding.case_id)[0]
    engine.process_next()
    final = engine.db.latest_assessment(binding.case_id)
    assert final["validation"]["valid"], final["validation"]
    case = engine.db.get_case(binding.case_id)
    assert case["readiness"] == "READY_FOR_PAYMENT"
    assert case["financial"]["amount_minor"] == 240000000
    assert case["financial"]["provider_status"] == "issued"
    assert not final["plan"]["actions"]
    replay, duplicate = engine.enqueue(binding.case_id, "INVESTIGATE", "original", {})
    assert duplicate and replay == event and not engine.process_next()
    assert engine.fixture.counts() == before


@pytest.mark.parametrize("change", ["sender", "project", "milestone", "stale", "agent"])
def test_bad_acceptance_requires_human_without_actions(loop, change):
    engine, binding = loop
    investigate(engine, binding)
    kwargs = {}
    if change == "sender": kwargs["sender"] = "outsider@example.com"
    if change == "project": kwargs["project"] = "UNRELATED-PROJECT"
    if change == "milestone": kwargs["scope"] = "unrelated-milestone"
    if change == "stale": kwargs["occurred_at"] = (datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
    item = arrival(binding, "migration-acceptance", **kwargs)
    if change == "agent": item["agent_generated"] = True
    engine.fixture.ingest_fixture_evidence(item)
    assessment = investigate(engine, binding)
    if change == "agent":
        assert engine.db.get_case(binding.case_id)["readiness"] != "READY_FOR_PAYMENT"
    else:
        assert not assessment["validation"]["valid"]
        assert not assessment["plan"]["actions"]
        assert engine.db.get_case(binding.case_id)["workflow_status"] == "NEEDS_OPERATOR"
    assert engine.fixture.counts()["mutations"] == 0


def test_new_evidence_invalidates_approval_before_any_write(loop):
    engine, binding = loop
    assessment = investigate(engine, binding)
    engine.fixture.ingest_fixture_evidence(arrival(binding, "migration-acceptance"))
    with pytest.raises(ValueError, match="stale"):
        engine.approve_and_execute(binding.case_id, assessment["id"], assessment["plan_hash"], "test")
    assert not engine.db.list_actions(binding.case_id)


def test_actual_response_loss_recovers_without_second_create(loop):
    engine, binding = loop
    assessment = investigate(engine, binding)
    engine.executor.lose_next_fixture_jira_response = True
    engine.executor._write = Mock(wraps=engine.executor._write)
    engine.approve_and_execute(binding.case_id, assessment["id"], assessment["plan_hash"], "test")
    jira = next(a for a in engine.db.list_actions(binding.case_id) if a["app"] == "jira")
    assert jira["verification_status"] == "VERIFIED"
    assert [a["outcome"] for a in jira["attempts"]] == ["UNCERTAIN", "FOUND_MATCH"]
    assert engine.fixture.counts()["issues"] == 1
    assert sum(call.args[0].app == "jira" for call in engine.executor._write.call_args_list) == 1


def test_crash_after_real_fixture_write_resumes_by_reconciliation(loop):
    engine, binding = loop
    assessment = investigate(engine, binding)
    original = engine.executor._write
    def crash(action, operation_ref):
        original(action, operation_ref)
        raise SystemExit("injected process death after remote create before acknowledgement")
    engine.executor._write = Mock(side_effect=crash)
    with pytest.raises(SystemExit, match="process death"):
        engine.approve_and_execute(binding.case_id, assessment["id"], assessment["plan_hash"], "test")
    assert engine.fixture.counts()["issues"] == 1
    restarted = WorkflowEngine(engine.db, engine.settings)
    restarted.executor._write = Mock(side_effect=AssertionError("Restart must not create"))
    assert restarted.executor.resume_pending()
    jira = next(a for a in engine.db.list_actions(binding.case_id) if a["app"] == "jira")
    assert jira["verification_status"] == "VERIFIED"
    restarted.executor._write.assert_not_called()
    assert restarted.fixture.counts()["issues"] == 1


@pytest.mark.parametrize("state", ["cancelled", "paid", "expired", "draft"])
def test_nonpayable_state_never_ready(loop, state):
    engine, binding = loop
    for cid in ("migration-acceptance", "training-change-order"):
        engine.fixture.ingest_fixture_evidence(arrival(binding, cid))
    data = engine.fixture._load(); data["financial"]["provider_status"] = state
    if state == "paid":
        data["financial"]["amount_paid_minor"] = data["financial"]["amount_minor"]
        data["financial"]["amount_due_minor"] = 0
    engine.fixture._save(data)
    investigate(engine, binding)
    assert engine.db.get_case(binding.case_id)["readiness"] != "READY_FOR_PAYMENT"


def test_provider_unavailable_produces_no_assessment_or_effect(loop):
    engine, binding = loop
    engine._collect = Mock(side_effect=TimeoutError("simulated unavailable provider"))
    engine.enqueue(binding.case_id, "INVESTIGATE", "unavailable", {})
    with pytest.raises(TimeoutError): engine.process_next()
    assert not engine.db.latest_assessment(binding.case_id)
    assert not engine.db.list_actions(binding.case_id)
    assert engine.db.get_case(binding.case_id)["workflow_status"] == "NEEDS_OPERATOR"


def test_retraction_invalidates_ready_and_reserves_nothing(loop):
    engine, binding = loop
    for cid in ("migration-acceptance", "training-change-order"):
        engine.fixture.ingest_fixture_evidence(arrival(binding, cid))
    investigate(engine, binding)
    assert engine.db.get_case(binding.case_id)["readiness"] == "READY_FOR_PAYMENT"
    engine.fixture.ingest_fixture_evidence(arrival(binding, "migration-acceptance", status="CONFLICT"))
    engine.poll_evidence(binding.case_id); engine.process_next()
    assert engine.db.get_case(binding.case_id)["readiness"] == "BLOCKED"
    assert engine.db.get_case(binding.case_id)["workflow_status"] == "NEEDS_OPERATOR"
    assert not engine.db.latest_assessment(binding.case_id)["plan"]["actions"]


def test_amount_change_requires_human_even_when_all_conditions_satisfied(loop):
    engine, binding = loop
    for cid in ("migration-acceptance", "training-change-order"):
        engine.fixture.ingest_fixture_evidence(arrival(binding, cid))
    data = engine.fixture._load(); financial = data["financial"]
    financial["lines"][0]["amount_minor"] += 100
    financial["amount_minor"] += 100; financial["amount_due_minor"] += 100
    engine.fixture._save(data)
    assessment = investigate(engine, binding)
    assert not assessment["validation"]["valid"]
    assert any("amount differs" in e for e in assessment["validation"]["errors"])
    assert engine.db.get_case(binding.case_id)["readiness"] != "READY_FOR_PAYMENT"


def test_model_cannot_use_failed_quote_to_satisfy_acceptance(loop):
    engine, binding = loop
    engine.fixture.ingest_fixture_evidence(arrival(binding, "training-change-order"))
    original = engine.reasoner.reason
    def corrupted(bundle):
        proposal, usage = original(bundle)
        condition = next(c for c in proposal.condition_evaluations if c.condition_id == "migration-acceptance")
        from app.models import ConditionStatus
        condition.status = ConditionStatus.SATISFIED
        condition.support_refs += condition.conflict_refs
        condition.conflict_refs = []
        return proposal, usage
    engine.reasoner.reason = corrupted
    assessment = investigate(engine, binding)
    assert not assessment["validation"]["valid"]
    assert engine.db.get_case(binding.case_id)["readiness"] != "READY_FOR_PAYMENT"


def test_failed_watch_invalidates_previously_ready_case(loop):
    engine, binding = loop
    for cid in ("migration-acceptance", "training-change-order"):
        engine.fixture.ingest_fixture_evidence(arrival(binding, cid))
    investigate(engine, binding)
    assert engine.db.get_case(binding.case_id)["readiness"] == "READY_FOR_PAYMENT"
    engine._collect = Mock(side_effect=TimeoutError("provider unavailable"))
    with pytest.raises(TimeoutError): engine.poll_evidence(binding.case_id)
    assert engine.db.get_case(binding.case_id)["readiness"] == "UNKNOWN"
    assert not engine.db.list_actions(binding.case_id)


@pytest.mark.parametrize("path", ["watch", "approve"])
def test_expired_acceptance_is_rejected_without_changed_source_or_model_call(loop, monkeypatch, path):
    engine, binding = loop
    if path == "watch":
        for cid in ("migration-acceptance", "training-change-order"):
            engine.fixture.ingest_fixture_evidence(arrival(binding, cid))
    assessment = investigate(engine, binding)
    from app import authority
    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)
    monkeypatch.setattr(authority, "datetime", FutureClock)
    engine.reasoner.reason = Mock(side_effect=AssertionError("No model call needed for expiry"))
    engine.executor._write = Mock(side_effect=AssertionError("No write permitted"))
    if path == "watch":
        assert engine.poll_evidence(binding.case_id) == (None, False)
    else:
        with pytest.raises(ValueError, match="acceptance evidence"):
            engine.approve_and_execute(binding.case_id, assessment["id"], assessment["plan_hash"], "test")
    assert engine.db.get_case(binding.case_id)["readiness"] == "UNKNOWN"
    assert not engine.db.list_actions(binding.case_id)
