from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import ROOT, Settings
from app.db import Database
from app.models import CaseBinding


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, CLEARDUE_MODE="fixture", CLEARDUE_DB_PATH=tmp_path / "test.db", CLEARDUE_SESSION_SECRET="test-secret", CLEARDUE_OPERATOR_PASSWORD="test")


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    database.initialize()
    fixture = json.loads((ROOT / "evals" / "fixtures" / "main_case.json").read_text(encoding="utf-8"))
    database.create_case(CaseBinding.model_validate(fixture["binding"]))
    return database

