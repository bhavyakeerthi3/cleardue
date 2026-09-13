from __future__ import annotations

import asyncio
import secrets
import uuid
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
    while not worker_stop.is_set():
        try:
            worked = await asyncio.to_thread(app.state.engine.process_next)
        except Exception:
            worked = True
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
        if origin and origin not in {"http://127.0.0.1:8000", "http://localhost:8000", "http://testserver"}:
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
        "mode": settings.mode,
        "reasoning_provider": settings.reasoning_provider,
        "reasoning_model": settings.gemini_model,
        "missing_live_config": settings.live_missing(),
        "fixture_counts": app.state.engine.fixture.counts() if app.state.engine.fixture else None,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": settings.mode,
        "live_ready": not settings.live_missing(),
        "reasoning_provider": settings.reasoning_provider,
        "reasoning_model": settings.gemini_model,
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
    return templates.TemplateResponse("case.html", {"request": request, "case_id": case_id, "csrf": request.session["csrf"], "mode": settings.mode})


@app.get("/evaluations", response_class=HTMLResponse)
def evaluations_page(request: Request, run_id: str = "latest"):
    require_operator(request)
    return templates.TemplateResponse("evaluations.html", {"request": request, "run_id": run_id, "csrf": request.session["csrf"]})


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
    action_ids = app.state.engine.approve_and_execute(case_id, body.assessment_id, body.plan_hash, operator)
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
    replay_id, duplicate = db.insert_event_once(event["case_id"], event["source"], event["dedupe_key"], event["event_type"], {})
    return {"event_id": replay_id, "duplicate": duplicate}


@app.post("/api/demo/faults")
def arm_fault(request: Request, body: FaultRequest, _: str = Depends(require_operator)):
    require_csrf(request)
    if settings.mode != "fixture":
        from .adapters.transport import FAULTS
        FAULTS.arm(body.provider, body.fault)
    else:
        app.state.engine.executor.lose_next_fixture_jira_response = True
    return {"armed": True, "provider": body.provider, "fault": body.fault}


@app.get("/api/evaluations/{run_id}")
def get_evaluations(request: Request, run_id: str, _: str = Depends(require_operator)):
    rows = db.list_evaluations(run_id)
    return {"run_id": run_id, "results": rows, "empty": not rows}
