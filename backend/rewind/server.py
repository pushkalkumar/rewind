"""FastAPI service: the HTTP API from docs/CONTRACT.md plus the built frontend (SPA fallback) when it exists.

Run from backend/:  python -m uvicorn rewind.server:app --port 8000
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import ROOT, SANDBOX_BACKEND, default_models
from .gitmine import parse_repo_url
from .models import Event, RunReport
from .runner import RunManager
from .sandbox import detect_backend

log = logging.getLogger("rewind.server")
DIST_DIR = Path(ROOT) / "frontend" / "dist"
KEEPALIVE_SECONDS = 15
ORPHAN_SWEEP_SECONDS = 5 * 60


def manager() -> RunManager:
    return RunManager.get()


def sweep_orphans() -> None:
    try:
        orphans = manager().recover_orphans()
    except Exception:
        log.exception("orphan sweep failed")
        return
    if orphans:
        log.info("marked %d orphaned run(s) as failed: %s", len(orphans), ", ".join(orphans))


def _sweep_loop(stop: threading.Event) -> None:
    """A run only counts as orphaned once it has been idle for a while, so keep looking after startup."""
    while not stop.wait(ORPHAN_SWEEP_SECONDS):
        sweep_orphans()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sweep_orphans()
    stop = threading.Event()
    threading.Thread(target=_sweep_loop, args=(stop,), name="orphan-sweep", daemon=True).start()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="Rewind", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class StartRun(BaseModel):
    repo_url: str
    models: Optional[list[str]] = None
    max_instances: Optional[int] = Field(default=None, ge=1)


# -- API -----------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "sandbox": detect_backend(SANDBOX_BACKEND), "models": default_models()}


@app.post("/api/runs")
def start_run(body: StartRun) -> dict:
    try:
        parse_repo_url(body.repo_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    run_id = manager().start_run(body.repo_url, body.models, body.max_instances)
    return {"id": run_id}


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return manager().list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> RunReport:
    report = manager().get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="run not found")
    return report


@app.get("/api/runs/{run_id}/events")
def get_events(run_id: str, after: int = 0) -> list[Event]:
    if manager().get_report(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return manager().get_events(run_id, after)


def sse_message(ev: Event) -> str:
    return "data: " + json.dumps(ev.model_dump(), ensure_ascii=False, default=str) + "\n\n"


def stream_events(run_id: str, after: int = 0) -> Iterator[str]:
    """Replay the events after seq `after`, then follow; a `: keepalive` comment every 15s while idle; ends on done/failed."""
    mgr = manager()
    q = mgr.subscribe(run_id, after)
    try:
        while True:
            try:
                ev = q.get(timeout=KEEPALIVE_SECONDS)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if ev is None:
                break
            yield sse_message(ev)
    finally:
        mgr.unsubscribe(run_id, q)


@app.get("/api/runs/{run_id}/stream")
def stream_run(run_id: str, after: int = 0) -> StreamingResponse:
    """SSE. `after` is the last seq the client already has, so a reconnect does not replay what it saw."""
    if manager().get_report(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream_events(run_id, after), media_type="text/event-stream", headers=headers)


# -- static frontend -------------------------------------------------------------

@app.get("/{path:path}", include_in_schema=False)
def spa(path: str, request: Request):
    """Serve frontend/dist with an index.html fallback so client-side routes like /print work."""
    if path.startswith("api/") or path == "api":
        raise HTTPException(status_code=404, detail="not found")
    if not DIST_DIR.is_dir():
        return JSONResponse({"ok": True, "message": "frontend/dist not built; API is at /api"}, status_code=200 if path == "" else 404)
    target = (DIST_DIR / path).resolve() if path else DIST_DIR / "index.html"
    if path and DIST_DIR.resolve() in target.parents and target.is_file():
        return FileResponse(str(target))
    return FileResponse(str(DIST_DIR / "index.html"))
