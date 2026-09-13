from __future__ import annotations

from app.workflow import WorkflowEngine


def test_investigation_builds_blocked_assessment(db, settings, tmp_path):
    engine = WorkflowEngine(db, settings)
    engine.fixture.state_path = tmp_path / "provider.json"
    engine.fixture.reset()
    event_id, duplicate = engine.enqueue(settings.demo_case_id, "INVESTIGATE", "source-1", {})
    assert duplicate is False
    engine.process_next()
    case = db.get_case(settings.demo_case_id)
    assessment = db.latest_assessment(settings.demo_case_id)
    assert case["readiness"] == "BLOCKED"
    assert case["workflow_status"] == "AWAITING_APPROVAL"
    assert assessment["validation"]["valid"] is True
    assert [d["decision"] for d in assessment["decisions"]] == ["SUPPORTED", "BLOCKED", "REVIEW_REQUIRED"]


def test_duplicate_event_is_suppressed(db, settings):
    engine = WorkflowEngine(db, settings)
    first, duplicate1 = engine.enqueue(settings.demo_case_id, "INVESTIGATE", "same-event", {})
    second, duplicate2 = engine.enqueue(settings.demo_case_id, "INVESTIGATE", "same-event", {})
    assert first == second
    assert duplicate1 is False and duplicate2 is True

