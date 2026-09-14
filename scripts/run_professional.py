"""Launch the new isolated fixture demonstration without touching the historical live case."""
import os
import sys
from pathlib import Path
import argparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--new-run", action="store_true", help="Start an isolated rehearsal; preserve all earlier runs")
    args = parser.parse_args()
    name = f"rehearsal-{uuid4().hex[:12]}.db" if args.new_run else "professional-fixture.db"
    os.environ.update(CLEARDUE_MODE="fixture", CLEARDUE_DB_PATH=str(ROOT/"data"/name), CLEARDUE_WATCH_ENABLED="true", CLEARDUE_WATCH_SECONDS="10")
    from app.config import Settings
    from app.db import Database
    from app.workflow import WorkflowEngine
    from app.demo_data import prepare_fixture
    settings = Settings(); db = Database(settings.db_path); db.initialize()
    if not db.get_case(settings.demo_case_id):
        prepare_fixture(WorkflowEngine(db, settings))
    print(f"FIXTURE ONLY — local provider simulator at http://127.0.0.1:{args.port}; no live writes")
    print(f"Rehearsal database: {settings.db_path}")
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__": main()
