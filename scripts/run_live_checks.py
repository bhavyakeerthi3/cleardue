from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db import Database
from app.workflow import WorkflowEngine


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute the approved three-provider test workflow")
    parser.add_argument("--execute", action="store_true", help="Required because this creates external test objects")
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("Refusing external writes without --execute")
    settings = get_settings()
    if settings.mode != "live" or settings.live_missing():
        raise SystemExit("Set CLEARDUE_MODE=live and complete scripts/preflight.py first")
    db = Database(settings.db_path)
    db.initialize()
    engine = WorkflowEngine(db, settings)
    event_id, _ = engine.enqueue(settings.demo_case_id, "INVESTIGATE", str(uuid.uuid4()), {})
    engine.process_next()
    assessment = db.latest_assessment(settings.demo_case_id)
    action_ids = engine.approve_and_execute(settings.demo_case_id, assessment["id"], assessment["plan_hash"], settings.operator_name)
    print(json.dumps({"event_id": event_id, "action_ids": action_ids, "actions": db.list_actions(settings.demo_case_id)}, indent=2, default=str))


if __name__ == "__main__":
    main()
