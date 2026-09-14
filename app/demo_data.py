"""Explicit, labeled local simulator data. Never used as live provider evidence."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from .config import ROOT
from .models import CaseBinding, EvidenceItem
from .reasoning import FixtureReasoner


def prepare_fixture(engine):
    if not engine.fixture:
        raise ValueError("Fixture setup is unavailable in live mode")
    fixture = json.loads((ROOT / "evals/fixtures/main_case.json").read_text(encoding="utf-8"))
    fixture["binding"]["case_id"] = engine.settings.demo_case_id
    proposal, _ = FixtureReasoner().reason({"evidence": fixture["evidence"]})
    now = (datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()
    for item in fixture["evidence"]:
        item["occurred_at"] = now
        item["retrieved_at"] = now
        item["content_hash"] = hashlib.sha256(item["content_text"].encode()).hexdigest()
        if item["app"] == "jira":
            item["identity"]["project_key"] = fixture["binding"]["jira_project_id"]
            item["content_text"] += f" Project {fixture['binding']['project_ref']}. Milestone: migration."
            item["content_hash"] = hashlib.sha256(item["content_text"].encode()).hexdigest()
    rules = []
    for condition in proposal.condition_evaluations:
        scope = "implementation" if condition.condition_id.startswith("implementation") else "training" if condition.condition_id.startswith("training") else "migration"
        rules.append({**condition.model_dump(mode="json", include={"condition_id", "kind", "line_item_ids", "requirement"}), "scope": scope, "source_app": "jira" if condition.kind == "DELIVERY" else "gmail", "max_age_seconds": 86400})
    fixture["binding"]["reviewed_conditions"] = rules
    fixture["binding"]["expected_amount_minor"] = fixture["financial"]["amount_minor"]
    fixture["binding"]["expected_currency"] = fixture["financial"]["currency"]
    source_path = engine.settings.db_path.with_suffix(".sources.json")
    source_path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    engine.fixture.source_path = source_path
    engine.fixture.reset()
    binding = CaseBinding.model_validate(fixture["binding"])
    engine.db.create_case(binding)
    return binding


def arrival(binding, condition_id, *, status="SATISFIED", sender=None, project=None, scope=None, event_id=None, occurred_at=None):
    scope = scope or ("training" if condition_id.startswith("training") else "migration")
    project = project or binding.project_ref
    text = (f"Project {project}. I confirm the {scope} milestone: " +
        ("the acceptance test passed and I give written acceptance." if scope == "migration" else "I approve the written training change order for billing.")
        if status == "SATISFIED" else f"Project {project}. The {scope} acceptance is withdrawn; the test failed.")
    at = occurred_at or datetime.now(timezone.utc).isoformat()
    identity = event_id or f"fixture-incoming-{condition_id}-{hashlib.sha256((text+at).encode()).hexdigest()[:12]}"
    return EvidenceItem(evidence_id=identity, external_id=identity, app="gmail", account_ref=binding.mailbox_ref,
        source_version="1", occurred_at=at, retrieved_at=at, identity={"from": sender or binding.participant_roles[0].email, "project_ref": project, "customer_id": binding.customer_id},
        content_text=text, content_hash=hashlib.sha256(text.encode()).hexdigest(), locator={"part": "text/plain"},
        metadata={"evidence_type": "customer_acceptance", "fixture_condition_id": condition_id, "fixture_status": status}).model_dump(mode="json")
