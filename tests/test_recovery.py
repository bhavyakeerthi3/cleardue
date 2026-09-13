from __future__ import annotations

import copy

from app.workflow import WorkflowEngine
from app.adapters.transport import FailureKind, ProviderError


def prepare(db, settings, tmp_path):
    engine = WorkflowEngine(db, settings)
    engine.fixture.state_path = tmp_path / "provider.json"
    engine.fixture.reset()
    engine.enqueue(settings.demo_case_id, "INVESTIGATE", "source", {})
    engine.process_next()
    return engine, db.latest_assessment(settings.demo_case_id)


def test_response_loss_reconciles_to_one_issue(db, settings, tmp_path):
    engine, assessment = prepare(db, settings, tmp_path)
    engine.executor.lose_next_fixture_jira_response = True
    engine.approve_and_execute(settings.demo_case_id, assessment["id"], assessment["plan_hash"], "tester")
    actions = db.list_actions(settings.demo_case_id)
    jira = next(a for a in actions if a["app"] == "jira")
    assert jira["verification_status"] == "VERIFIED"
    assert engine.fixture.counts()["issues"] == 1


def test_reapproval_creates_no_duplicate_effects(db, settings, tmp_path):
    engine, assessment = prepare(db, settings, tmp_path)
    engine.approve_and_execute(settings.demo_case_id, assessment["id"], assessment["plan_hash"], "tester")
    before = engine.fixture.counts()
    engine.approve_and_execute(settings.demo_case_id, assessment["id"], assessment["plan_hash"], "tester")
    assert engine.fixture.counts() == before


def test_verified_gmail_draft_is_reused_when_model_purpose_wording_changes(db, settings, tmp_path):
    engine, assessment = prepare(db, settings, tmp_path)
    engine.approve_and_execute(
        settings.demo_case_id, assessment["id"], assessment["plan_hash"], "tester"
    )
    existing = db.list_actions(settings.demo_case_id)
    gmail = next(action for action in existing if action["app"] == "gmail")
    revised = copy.deepcopy(assessment)
    planned_gmail = next(action for action in revised["plan"]["actions"] if action["app"] == "gmail")
    planned_gmail["logical_target"] = "draft:different-model-wording"

    reserved = engine.executor.reserve_plan(db.get_case(settings.demo_case_id), revised)

    assert gmail["id"] in reserved
    assert len(db.list_actions(settings.demo_case_id)) == len(existing)


def test_failed_readback_retries_verification_without_another_create(db, settings, tmp_path, monkeypatch):
    engine, assessment = prepare(db, settings, tmp_path)
    action_ids = engine.executor.reserve_plan(db.get_case(settings.demo_case_id), assessment)
    jira_id = next(x for x in action_ids if db.get_action(x)["app"] == "jira")
    original_verify = engine.fixture.verify_jira
    calls = []
    from unittest.mock import Mock
    write = Mock(wraps=engine.executor._write)
    monkeypatch.setattr(engine.executor, "_write", write)

    def flaky_verify(*args):
        calls.append(args[0])
        if len(calls) <= 2:
            raise ProviderError(FailureKind.TRANSIENT_READ, "jira", "read-back timeout")
        return original_verify(*args)

    monkeypatch.setattr(engine.fixture, "verify_jira", flaky_verify)
    engine.executor.execute(jira_id)
    external_id = db.get_action(jira_id)["external_id"]
    assert external_id
    assert db.get_action(jira_id)["request_status"] == "RETRY_WAIT"
    engine.executor.execute(jira_id)
    engine.executor.execute(jira_id)
    assert engine.fixture.counts()["issues"] == 1
    assert calls == [external_id] * 3
    assert write.call_count == 1
    assert db.get_action(jira_id)["verification_status"] == "VERIFIED"
