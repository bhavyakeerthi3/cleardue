from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db import Database
from app.workflow import WorkflowEngine


def main() -> None:
    settings = get_settings()
    if settings.mode != "live":
        raise SystemExit("Set CLEARDUE_MODE=live")
    missing = settings.live_missing()
    if missing:
        raise SystemExit("Missing live configuration: " + ", ".join(missing))

    db = Database(settings.db_path)
    db.initialize()
    before_actions = len(db.list_actions(settings.demo_case_id))
    assessment_id = WorkflowEngine(db, settings).investigate(settings.demo_case_id)
    assessment = db.latest_assessment(settings.demo_case_id)
    evidence = db.list_case_evidence(settings.demo_case_id)
    after_actions = len(db.list_actions(settings.demo_case_id))
    print(json.dumps({
        "assessment_id": assessment_id,
        "model_id": assessment["model_id"],
        "validation": assessment["validation"],
        "evidence_counts": {
            app: sum(item["app"] == app for item in evidence)
            for app in ("gmail", "jira")
        },
        "readiness": db.get_case(settings.demo_case_id)["readiness"],
        "validated_actions": [
            {"app": action["app"], "action_type": action["action_type"]}
            for action in assessment["plan"]["actions"]
        ],
        "external_writes_attempted": after_actions > before_actions,
    }, indent=2))


if __name__ == "__main__":
    main()
