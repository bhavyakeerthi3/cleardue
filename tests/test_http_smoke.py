"""Real ASGI routes/templates, isolated fixture DB, no live credentials or clients."""
import importlib
import re
import sys

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.demo_data import prepare_fixture
from app.workflow import WorkflowEngine


def test_console_http_csrf_and_stale_approval(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, CLEARDUE_MODE="fixture", CLEARDUE_DB_PATH=tmp_path/"http.db", CLEARDUE_WATCH_ENABLED=False)
    db = Database(settings.db_path); db.initialize()
    binding = prepare_fixture(WorkflowEngine(db, settings))
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    try:
        with TestClient(main.app) as client:
            page = client.get(f"/cases/{binding.case_id}")
            assert page.status_code == 200 and "WHAT'S BLOCKING PAYMENT?" in page.text
            csrf = re.search(r'data-csrf="([^"]+)"', page.text)[1]
            route = f"/api/cases/{binding.case_id}"
            assert client.get(route).json()["mode"] == "fixture"
            assert client.get("/health").json()["reasoning_provider"] == "fixture-rules"
            assert client.post(route+"/investigate").status_code == 403
            assert client.post(route+"/investigate", headers={"Origin":"https://untrusted.example", "x-csrf-token":csrf}).status_code == 403
            assert client.post(route+"/approve", headers={"x-csrf-token":csrf}, json={"assessment_id":"stale", "plan_hash":"stale"}).status_code == 409
            assert client.get("/evaluations").status_code == 200
            assert not db.list_actions(binding.case_id)
    finally:
        sys.modules.pop("app.main", None)
