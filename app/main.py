from __future__ import annotations

import asyncio
import secrets
import uuid
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import ROOT, get_settings
from .db import Database
from .models import ApprovalRequest, FaultRequest
from .workflow import WorkflowEngine


settings = get_settings()
db = Database(settings.db_path)
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))
worker_stop = asyncio.Event()


async def durable_worker(app: FastAPI) -> None:
    last_poll = 0.0
    while not worker_stop.is_set():
        try:
            worked = await asyncio.to_thread(app.state.engine.process_next)
            if not worked:
                worked = await asyncio.to_thread(app.state.engine.executor.resume_pending)
            if settings.watch_enabled and not worked and time.monotonic() - last_poll >= settings.watch_interval_seconds:
                last_poll = time.monotonic()
                await asyncio.to_thread(app.state.engine.poll_evidence, settings.demo_case_id)
        except Exception:
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(worker_stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.initialize()
    app.state.engine = WorkflowEngine(db, settings)
    worker_stop.clear()
    task = asyncio.create_task(durable_worker(app))
    yield
    worker_stop.set()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="ClearDue", version="0.1.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="strict", https_only=False)
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")


@app.middleware("http")
async def fixed_host_origin(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0]
    if host not in {"127.0.0.1", "localhost", "testserver"}:
        return JSONResponse({"detail": "ClearDue is loopback-only"}, status_code=400)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "invalid origin"}, status_code=403)
    return await call_next(request)


def require_operator(request: Request) -> str:
    operator = request.session.get("operator")
    if not operator:
        operator = settings.operator_name
        request.session["operator"] = operator
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return str(operator)


def require_csrf(request: Request) -> None:
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if not expected or not supplied or not secrets.compare_digest(str(expected), supplied):
        raise HTTPException(403, "invalid CSRF token")


def case_view(case_id: str) -> dict[str, Any]:
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(404, "case not found")
    assessment = db.latest_assessment(case_id)
    return {
        "case": case,
        "assessment": assessment,
        "evidence": db.list_case_evidence(case_id),
        "actions": db.list_actions(case_id),
        "activity": db.activity(case_id),
        "mode": settings.mode,
        "reasoning_provider": "fixture-rules" if settings.mode == "fixture" else settings.reasoning_provider,
        "reasoning_model": "fixture-rules" if settings.mode == "fixture" else settings.gemini_model,
        "watch_enabled": settings.watch_enabled,
        "missing_live_config": settings.live_missing(),
        "fixture_counts": app.state.engine.fixture.counts() if app.state.engine.fixture else None,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": settings.mode,
        "live_ready": not settings.live_missing(),
        "reasoning_provider": "fixture-rules" if settings.mode == "fixture" else settings.reasoning_provider,
        "reasoning_model": "fixture-rules" if settings.mode == "fixture" else settings.gemini_model,
    }


@app.get("/login")
def login_page(request: Request):
    require_operator(request)
    return RedirectResponse(f"/cases/{settings.demo_case_id}", status_code=303)


@app.post("/login")
def login(request: Request):
    require_operator(request)
    return RedirectResponse(f"/cases/{settings.demo_case_id}", status_code=303)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(f"/cases/{settings.demo_case_id}")


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_page(request: Request, case_id: str):
    require_operator(request)
    return templates.TemplateResponse(request=request, name="case.html", context={"request": request, "case_id": case_id, "csrf": request.session["csrf"], "mode": settings.mode})


@app.get("/evaluations", response_class=HTMLResponse)
def evaluations_page(request: Request, run_id: str = "latest"):
    require_operator(request)
    return templates.TemplateResponse(request=request, name="evaluations.html", context={"request": request, "run_id": run_id, "csrf": request.session["csrf"]})


@app.get("/api/cases/{case_id}")
def get_case_api(request: Request, case_id: str, _: str = Depends(require_operator)):
    return case_view(case_id)


@app.post("/api/cases/{case_id}/investigate", status_code=202)
def investigate(request: Request, case_id: str, _: str = Depends(require_operator)):
    require_csrf(request)
    event_id, duplicate = app.state.engine.enqueue(case_id, "INVESTIGATE", str(uuid.uuid4()), {})
    return {"event_id": event_id, "duplicate": duplicate}


@app.post("/api/cases/{case_id}/refresh", status_code=202)
def refresh(request: Request, case_id: str, _: str = Depends(require_operator)):
    require_csrf(request)
    event_id, duplicate = app.state.engine.enqueue(case_id, "REFRESH", str(uuid.uuid4()), {})
    return {"event_id": event_id, "duplicate": duplicate}


@app.post("/api/cases/{case_id}/approve")
def approve(request: Request, case_id: str, body: ApprovalRequest, operator: str = Depends(require_operator)):
    require_csrf(request)
    try:
        action_ids = app.state.engine.approve_and_execute(case_id, body.assessment_id, body.plan_hash, operator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"action_ids": action_ids}


@app.post("/api/demo/events/{event_id}/replay")
def replay(request: Request, event_id: str, _: str = Depends(require_operator)):
    require_csrf(request)
    with db.connection() as conn:
        event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        raise HTTPException(404, "event not found")
    if event["status"] != "COMPLETED":
        raise HTTPException(409, "only a completed event can be replayed")
    before_rows = len(db.list_actions(event["case_id"]))
    before_counts = app.state.engine.fixture.counts() if app.state.engine.fixture else None
    replay_id, duplicate = db.insert_event_once(event["case_id"], event["source"], event["dedupe_key"], event["event_type"], {})
    proof = {"event_id": replay_id, "duplicate": duplicate, "ledger_rows": len(db.list_actions(event["case_id"])), "fixture_counts": app.state.engine.fixture.counts() if app.state.engine.fixture else None}
    proof["before"] = {"ledger_rows": before_rows, "fixture_counts": before_counts}
    proof["additional_ledger_rows"] = proof["ledger_rows"] - before_rows
    proof["additional_fixture_mutations"] = proof["fixture_counts"]["mutations"] - before_counts["mutations"] if before_counts else None
    db.record_activity(event["case_id"], "REPLAY", proof)
    return proof


@app.post("/api/cases/{case_id}/fixture-evidence/{scenario}")
def fixture_evidence(request: Request, case_id: str, scenario: str, _: str = Depends(require_operator)):
    require_csrf(request)
    if settings.mode != "fixture":
        raise HTTPException(403, "Simulated evidence is fixture-only")
    if scenario not in {"migration", "training", "unauthorized"}:
        raise HTTPException(400, "unknown fixture scenario")
    from .demo_data import arrival
    from .models import CaseBinding
    binding = CaseBinding.model_validate(db.get_case(case_id)["binding"])
    cid = "training-change-order" if scenario == "training" else "migration-acceptance"
    item = arrival(binding, cid, sender="outsider@example.com" if scenario == "unauthorized" else None)
    app.state.engine.fixture.ingest_fixture_evidence(item)
    db.record_activity(case_id, "FIXTURE_EVIDENCE_ARRIVED", {"scenario": scenario, "source_id": item["external_id"], "mode": "fixture"})
    # The same evidence watcher used by live mode detects the changed snapshot.
    event, duplicate = app.state.engine.poll_evidence(case_id)
    return {"event_id": event, "duplicate": duplicate, "mode": "fixture"}


@app.post("/api/demo/faults")
def arm_fault(request: Request, body: FaultRequest, _: str = Depends(require_operator)):
    require_csrf(request)
    if settings.mode != "fixture":
        raise HTTPException(403, "UI fault injection is fixture-only")
    app.state.engine.executor.lose_next_fixture_jira_response = True
    return {"armed": True, "provider": body.provider, "fault": body.fault}


@app.get("/api/evaluations/{run_id}")
def get_evaluations(request: Request, run_id: str, _: str = Depends(require_operator)):
    rows = db.list_evaluations(run_id)
    return {"run_id": run_id, "results": rows, "empty": not rows}
