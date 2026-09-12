"""Run orchestration: clone -> mine -> verify -> benchmark -> score, emitting events and persisting as it goes.

`RunManager` is a process-wide singleton. `start_run` launches one thread per run; the in-memory RunReport is
kept current so GET /api/runs/{id} always reflects what has arrived, and every event fans out to SSE subscribers.
Runs owned by another process (e.g. `cli.py bench`) are followed by polling the database instead.

The clone -> mine -> verify stages of concurrent runs on the same repo are serialized with a per-repo lock, since
they share one cached clone and one venv; benchmarking works on per-run exported trees and runs unlocked.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import db
from .agent import run_agent
from .config import MAX_INSTANCES, SANDBOX_BACKEND, WORK_DIR, default_models, model_label
from .events import EventSink
from .gitmine import clone_repo, find_candidates, parse_repo_url, scanned_count
from .llm import create_driver
from .models import Event, MinedStats, RunReport, RunStatus
from .sandbox import SandboxSpec, create_sandbox, detect_backend
from .scoring import apply_cheat, compute_scores
from .verify import build_instances, prepare_env

log = logging.getLogger("rewind.runner")

_STATUSES = {"queued", "cloning", "mining", "verifying", "benchmarking", "done", "failed"}
TERMINAL = ("done", "failed")
_END = None  # queue sentinel

DB_POLL_SECONDS = 1.0  # how often a stream for a run owned by another process re-reads the db
DB_POLL_IDLE_LIMIT = 30 * 60  # give up following such a run after this long without a new event
ORPHAN_GRACE_SECONDS = 15 * 60  # a non-terminal run idle for longer than this is presumed dead
WAITING_FOR_REPO = "waiting for another run on this repo"

_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def repo_lock(repo_name: str) -> threading.Lock:
    """The process-wide lock serializing clone/mine/verify for one cached repo checkout."""
    with _repo_locks_guard:
        lock = _repo_locks.get(repo_name)
        if lock is None:
            lock = _repo_locks[repo_name] = threading.Lock()
        return lock


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def stats_line(stats: MinedStats) -> str:
    reasons = ", ".join(f"{k}: {v}" for k, v in sorted(stats.discard_reasons.items(), key=lambda kv: -kv[1]))
    line = f"Mined {stats.commits_scanned} commits, {stats.candidates} candidates, {stats.verified} verified, {stats.benchmarked} benchmarked"
    return line + (f" (discarded: {reasons})" if reasons else "")


class RunState:
    """Everything the manager keeps in memory for one run."""

    def __init__(self, report: RunReport):
        self.report = report
        self.events: list[Event] = []
        self.subscribers: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.finished = threading.Event()


class RunManager:
    _instance: Optional["RunManager"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._pollers: dict[queue.Queue, threading.Event] = {}  # db-following subscriber queue -> its stop flag
        self._lock = threading.Lock()
        db.init_db()

    @classmethod
    def get(cls) -> "RunManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # -- public API ----------------------------------------------------------
    def start_run(
        self,
        repo_url: str,
        models: Optional[list[str]] = None,
        max_instances: Optional[int] = None,
        on_event: Optional[Callable[[Event], None]] = None,
    ) -> str:
        """Create the run row and start its worker thread; returns the 8-hex run id."""
        model_ids = [m.strip() for m in (models or []) if m.strip()] or default_models()
        report = RunReport(
            id=new_run_id(),
            repo_url=repo_url.strip(),
            status="queued",
            created_at=now_iso(),
            sandbox_backend=detect_backend(SANDBOX_BACKEND),
            model_ids=model_ids,
        )
        state = RunState(report)
        with self._lock:
            self._runs[report.id] = state
        db.upsert_run(report)
        n = max_instances if max_instances and max_instances > 0 else MAX_INSTANCES
        state.thread = threading.Thread(target=self._run, args=(state, model_ids, n, on_event), name=f"run-{report.id}", daemon=True)
        state.thread.start()
        return report.id

    def wait(self, run_id: str, timeout: Optional[float] = None) -> bool:
        state = self._runs.get(run_id)
        return state.finished.wait(timeout) if state else True

    def get_report(self, run_id: str) -> Optional[RunReport]:
        state = self._runs.get(run_id)
        if state is None:
            return db.get_run(run_id)
        with state.lock:
            return state.report.model_copy(deep=True)

    def get_events(self, run_id: str, after: int = 0) -> list[Event]:
        state = self._runs.get(run_id)
        if state is None:
            return db.get_events(run_id, after)
        with state.lock:
            return [ev for ev in state.events if ev.seq > after]

    def list_runs(self) -> list[dict]:
        return db.list_runs()

    def is_live(self, run_id: str) -> bool:
        """True when this process owns the run's worker thread."""
        return run_id in self._runs

    def subscribe(self, run_id: str, after: int = 0) -> "queue.Queue[Optional[Event]]":
        """Queue that replays the events with seq > `after`, then receives live ones; `None` marks the end.

        A run this process does not own is followed by polling the database (see `_poll_db`) until its
        done/failed event lands; call `unsubscribe` to stop that poller when the consumer goes away.
        """
        q: "queue.Queue[Optional[Event]]" = queue.Queue()
        state = self._runs.get(run_id)
        if state is None:
            status = db.get_run_status(run_id)
            events = db.get_events(run_id, after)
            for ev in events:
                q.put(ev)
            if status is None or status in TERMINAL or (events and events[-1].type in TERMINAL):
                q.put(_END)
                return q
            stop = threading.Event()
            with self._lock:
                self._pollers[q] = stop
            last = events[-1].seq if events else after
            threading.Thread(target=self._poll_db, args=(run_id, q, last, stop), name=f"poll-{run_id}", daemon=True).start()
            return q
        with state.lock:
            for ev in state.events:
                if ev.seq > after:
                    q.put(ev)
            if state.finished.is_set():
                q.put(_END)
            else:
                state.subscribers.append(q)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue) -> None:
        with self._lock:
            stop = self._pollers.pop(q, None)
        if stop is not None:
            stop.set()
        state = self._runs.get(run_id)
        if state is None:
            return
        with state.lock:
            if q in state.subscribers:
                state.subscribers.remove(q)

    def _poll_db(self, run_id: str, q: queue.Queue, after: int, stop: threading.Event) -> None:
        """Feed `q` with new db events for a run owned by another process; ends on done/failed or after idling."""
        idle_since = time.monotonic()
        try:
            while not stop.is_set():
                # Read the status before the events: a terminal status means the terminal event is already stored,
                # so a status-terminal + no-new-events exit can never skip that event.
                status = db.get_run_status(run_id)
                events = db.get_events(run_id, after)
                for ev in events:
                    q.put(ev)
                    after = ev.seq
                if events:
                    idle_since = time.monotonic()
                    if events[-1].type in TERMINAL:
                        return
                elif status is None or status in TERMINAL or time.monotonic() - idle_since > DB_POLL_IDLE_LIMIT:
                    return
                stop.wait(DB_POLL_SECONDS)
        except Exception:
            log.exception("db poller for run %s failed", run_id)
        finally:
            q.put(_END)
            with self._lock:
                self._pollers.pop(q, None)

    def recover_orphans(self, now: Optional[float] = None) -> list[str]:
        """Mark non-terminal runs nobody is working on as failed so their streams terminate cleanly.

        A run is presumed dead once its last activity (created_at + the last event's t) is older than
        ORPHAN_GRACE_SECONDS; younger ones are left alone since another process may still own them.
        """
        now = time.time() if now is None else now
        fixed: list[str] = []
        for row in db.list_runs():
            if row["status"] in TERMINAL or row["id"] in self._runs:
                continue
            report = db.get_run(row["id"])
            if report is None:
                continue
            last = db.last_event(report.id)
            if last is not None and last.type in TERMINAL:
                continue  # the owner is mid-way through _finish; the row will follow
            if now - last_activity(report.created_at, last) < ORPHAN_GRACE_SECONDS:
                continue
            report.status = "failed"
            report.error = "the run stopped reporting progress (its process exited?)"
            report.finished_at = now_iso()
            seq = (last.seq + 1) if last else 1
            t = last.t if last else 0.0
            db.insert_event(report.id, Event(seq=seq, t=t, type="failed", data={"error": report.error}))
            db.upsert_run(report)
            fixed.append(report.id)
        return fixed

    # -- internals -------------------------------------------------------------
    def _on_event(self, state: RunState, ev: Event, on_event: Optional[Callable[[Event], None]]) -> None:
        report = state.report
        with state.lock:
            if ev.type == "status" and ev.data.get("status") in _STATUSES:
                report.status = ev.data["status"]
            elif ev.type == "clone.done":
                report.repo_name = ev.data.get("repo_name", report.repo_name)
            elif ev.type == "mine.scan":
                report.stats.commits_scanned += 1
            elif ev.type == "mine.candidate":
                report.stats.candidates += 1
            state.events.append(ev)
            subscribers = list(state.subscribers)
        db.insert_event(report.id, ev)
        db.upsert_run(report)
        for q in subscribers:
            q.put(ev)
        if on_event is not None:
            try:
                on_event(ev)
            except Exception:
                log.exception("on_event hook failed")

    def _finish(self, state: RunState, sink: EventSink, status: RunStatus, error: Optional[str] = None) -> None:
        """Bring the run to a terminal status. Whatever fails in here, the run ends and every subscriber gets the sentinel."""
        report = state.report
        try:
            with state.lock:
                report.status = status
                report.error = error
                report.finished_at = now_iso()
            try:
                if status == "failed":
                    sink.emit("failed", error=error or "unknown error")
                else:
                    sink.emit("done", report=report.model_dump())
            except Exception as e:
                log.exception("run %s: emitting the terminal event failed", report.id)
                with state.lock:
                    report.status = "failed"
                    report.error = f"{type(e).__name__}: {e}"
                if status != "failed":
                    try:
                        sink.emit("failed", error=report.error)
                    except Exception:
                        log.exception("run %s: emitting the fallback failed event failed too", report.id)
            try:
                db.upsert_run(report)
            except Exception:
                log.exception("run %s: persisting the final report failed", report.id)
        finally:
            # `finished` is set under the run lock before the subscriber list is cleared, so a subscriber arriving
            # concurrently either gets the sentinel from the snapshot below or sees `finished` in `subscribe`.
            with state.lock:
                state.finished.set()
                subscribers = list(state.subscribers)
                state.subscribers.clear()
            for q in subscribers:
                q.put(_END)

    def _run(self, state: RunState, model_ids: list[str], max_instances: int, on_event: Optional[Callable[[Event], None]]) -> None:
        report = state.report
        sink = EventSink(on_event=lambda ev: self._on_event(state, ev, on_event))
        try:
            execute_run(report, sink, model_ids, max_instances, state)
            self._finish(state, sink, "done")
        except Exception as e:
            log.error("run %s failed:\n%s", report.id, traceback.format_exc())
            self._finish(state, sink, "failed", f"{type(e).__name__}: {e}")


def last_activity(created_at: str, last: Optional[Event]) -> float:
    """Unix time of a run's most recent sign of life: created_at plus the last event's offset. Unparseable -> 0."""
    try:
        started = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started.timestamp() + (last.t if last is not None else 0.0)


def execute_run(report: RunReport, sink: EventSink, model_ids: list[str], max_instances: int, state: Optional[RunState] = None) -> RunReport:
    """The pipeline for one run. Mutates `report` in place (instances, stats, results, scores) and returns it."""
    lock = state.lock if state is not None else threading.Lock()
    run_dir = Path(WORK_DIR) / "runs" / report.id
    trees_dir = run_dir / "trees"

    owner, name, _url = parse_repo_url(report.repo_url)
    shared = repo_lock(f"{owner}__{name}")
    if not shared.acquire(blocking=False):
        sink.emit("status", status="queued", message=WAITING_FOR_REPO)
        shared.acquire()
    try:
        repo = clone_repo(report.repo_url, sink=sink)
        with lock:
            report.repo_name = repo.name
        candidates = find_candidates(repo, sink=sink)
        commits_scanned = scanned_count(repo)
        if not candidates:
            stats = MinedStats(commits_scanned=commits_scanned)
            with lock:
                report.instances = []
                report.stats = stats
                report.scores = []
            sink.emit("mine.done", stats=stats.model_dump())
            sink.emit("status", status="mining", message=f"no candidates matched the fix heuristic in {commits_scanned} commits. {stats_line(stats)}")
            sink.emit("scores", scores=[])
            return report
        env = prepare_env(repo, sink=sink)
        instances, stats = build_instances(repo, env, candidates, max_instances=max_instances, trees_dir=trees_dir, sink=sink, commits_scanned=commits_scanned)
    finally:
        shared.release()
    with lock:
        report.instances = instances
        report.stats = stats

    if not instances:
        sink.emit("status", status="benchmarking", message=f"no verified instances; nothing to benchmark. {stats_line(stats)}")
        with lock:
            report.scores = []
        sink.emit("scores", scores=[])
        return report

    backend = detect_backend(SANDBOX_BACKEND)
    with lock:
        report.sandbox_backend = backend
    sink.emit("status", status="benchmarking", message=f"{len(instances)} instances x {len(model_ids)} models on {backend} sandbox")
    drivers = {mid: create_driver(mid) for mid in model_ids}
    for inst in instances:
        tree = trees_dir / inst.id
        for mid in model_ids:
            sink.emit("bench.start", instance_id=inst.id, model_id=mid, model_label=model_label(mid))
            t0 = time.time()
            sandbox = create_sandbox(SandboxSpec(tree=tree, venv_python=env.venv_python), backend)
            try:
                result = run_agent(inst, sandbox, drivers[mid], sink=sink)
            finally:
                sandbox.close()
            result.model_id = mid
            result.model_label = model_label(mid)
            apply_cheat(result)
            with lock:
                report.results.append(result)
            log.info("bench %s/%s: fixed=%s cheated=%s broke=%s in %.1fs", inst.id, mid, result.fixed, result.cheated, result.broke, time.time() - t0)
            sink.emit("bench.result", result=result.model_dump())

    scores = compute_scores(report.results, model_ids)
    with lock:
        report.scores = scores
    sink.emit("scores", scores=[s.model_dump() for s in scores])
    return report
