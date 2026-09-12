"""Server + db + runner tests: health, db roundtrip, events/stream endpoints (replay after a seq, following a run
owned by another process), the zero-candidate path, the per-repo lock, orphan recovery and a crash-proof _finish."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from rewind import db, runner
from rewind.events import EventSink
from rewind.gitmine import RepoCheckout
from rewind.models import AgentResult, Candidate, Event, Instance, ModelScore, RunReport
from rewind.runner import WAITING_FOR_REPO, RunManager
from rewind.server import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    db.set_path(tmp_path / "runs.sqlite")
    monkeypatch.setattr(RunManager, "_instance", None)
    with TestClient(app) as c:
        yield c
    db.set_path(tmp_path / "runs.sqlite")


@pytest.fixture
def manager(tmp_path: Path, monkeypatch) -> RunManager:
    db.set_path(tmp_path / "runs.sqlite")
    monkeypatch.setattr(RunManager, "_instance", None)
    return RunManager.get()


def wait_for(cond, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def fake_checkout(repo_url: str) -> RepoCheckout:
    owner, name = repo_url.rstrip("/").rsplit("/", 2)[-2:]
    return RepoCheckout(url=repo_url, name=f"{owner}__{name}", path=Path("nowhere"), default_branch="main", head_sha="c" * 40)


def stub_pipeline(monkeypatch, commits: int = 250, clone_hook=None):
    """Make execute_run's mining stages instant: fake clone, no candidates, and a prepare_env that must not run."""
    calls: dict[str, int] = {"prepare_env": 0}

    def clone_repo(repo_url, work_dir=None, sink=None):
        if clone_hook is not None:
            clone_hook(repo_url)
        repo = fake_checkout(repo_url)
        sink.emit("clone.done", repo_name=repo.name, commits=commits)
        return repo

    def find_candidates(repo, max_commits=400, sink=None):
        sink.emit("status", status="mining", message="scanning")
        return []

    def prepare_env(repo, sink=None):
        calls["prepare_env"] += 1
        raise AssertionError("prepare_env must not be called when there are no candidates")

    monkeypatch.setattr(runner, "clone_repo", clone_repo)
    monkeypatch.setattr(runner, "find_candidates", find_candidates)
    monkeypatch.setattr(runner, "scanned_count", lambda repo, max_commits=400: commits)
    monkeypatch.setattr(runner, "prepare_env", prepare_env)
    return calls


def stream_seqs(client: TestClient, run_id: str, after: int = 0) -> list[Event]:
    params = {"after": after} if after else None
    with client.stream("GET", f"/api/runs/{run_id}/stream", params=params) as r:
        assert r.status_code == 200
        lines = [line for line in r.iter_lines() if line.startswith("data: ")]
    return [Event.model_validate(json.loads(line[len("data: "):])) for line in lines]


def synthetic_report(run_id: str = "abcd1234") -> RunReport:
    cand = Candidate(sha="a" * 40, parent_sha="b" * 40, subject="Fix off-by-one in slicer", message="Fix off-by-one in slicer\n\nCloses #12",
                     author_date="2025-01-01T00:00:00+00:00", test_files=["tests/test_slice.py"], source_files=["src/pkg/slice.py"])
    inst = Instance(id="aaaaaaaaaa", candidate=cand, issue_text="Fix off-by-one in slicer", fail_to_pass=["tests/test_slice.py::test_end"],
                    pass_to_pass=["tests/test_slice.py::test_start"], test_patch="diff --git a/tests/test_slice.py b/tests/test_slice.py\n",
                    gold_patch="diff --git a/src/pkg/slice.py b/src/pkg/slice.py\n", test_files=["tests/test_slice.py"], verify_seconds=3.2)
    result = AgentResult(instance_id=inst.id, model_id="claude-cli:haiku", model_label="Claude Haiku 4.5", steps_used=4,
                         diff="diff --git a/src/pkg/slice.py b/src/pkg/slice.py\n@@ -1 +1 @@\n-x\n+y\n", changed_files=["src/pkg/slice.py"],
                         fixed=True, f2p_results={"tests/test_slice.py::test_end": "passed"}, seconds=12.5, final_message="fixed the bound")
    score = ModelScore(model_id="claude-cli:haiku", model_label="Claude Haiku 4.5", n=1, fixed=1, cheated=0, broke=0, honest=1, score=1.0, grade="A")
    return RunReport(id=run_id, repo_url="https://github.com/example/pkg", repo_name="example__pkg", status="done",
                     created_at="2025-01-01T00:00:00+00:00", finished_at="2025-01-01T00:05:00+00:00", sandbox_backend="local",
                     model_ids=["claude-cli:haiku"], instances=[inst], results=[result], scores=[score])


def synthetic_events(report: RunReport) -> list[Event]:
    return [
        Event(seq=1, t=0.0, type="status", data={"status": "cloning", "message": "cloning example/pkg"}),
        Event(seq=2, t=1.5, type="clone.done", data={"repo_name": "example__pkg", "commits": 100}),
        Event(seq=3, t=2.0, type="mine.candidate", data={"sha": "a" * 40, "subject": "Fix off-by-one in slicer", "test_files": ["tests/test_slice.py"], "source_files": ["src/pkg/slice.py"]}),
        Event(seq=4, t=9.0, type="verify.keep", data={"sha": "a" * 40, "subject": "Fix off-by-one in slicer", "fail_to_pass": ["tests/test_slice.py::test_end"], "pass_to_pass_count": 1, "seconds": 3.2}),
        Event(seq=5, t=22.0, type="bench.result", data={"result": report.results[0].model_dump()}),
        Event(seq=6, t=22.1, type="scores", data={"scores": [s.model_dump() for s in report.scores]}),
        Event(seq=7, t=22.2, type="done", data={"report": report.model_dump()}),
    ]


def store(report: RunReport) -> list[Event]:
    db.upsert_run(report)
    events = synthetic_events(report)
    for ev in events:
        db.insert_event(report.id, ev)
    return events


def test_health(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["sandbox"] in ("local", "docker", "daytona")
    assert isinstance(body["models"], list) and body["models"]


def test_db_roundtrip(client: TestClient):
    report = synthetic_report()
    events = store(report)
    assert db.get_run(report.id) == report
    assert db.get_run("nope") is None
    got = db.get_events(report.id)
    assert got == events
    assert [e.seq for e in db.get_events(report.id, after=5)] == [6, 7]
    rows = db.list_runs()
    assert rows == [{"id": report.id, "repo_url": report.repo_url, "status": "done", "created_at": report.created_at}]
    report.status = "failed"
    report.error = "boom"
    db.upsert_run(report)
    assert db.get_run(report.id).error == "boom"
    assert db.list_runs()[0]["status"] == "failed"


def test_run_endpoints(client: TestClient):
    report = synthetic_report()
    events = store(report)
    assert client.get("/api/runs").json()[0]["id"] == report.id
    got = client.get(f"/api/runs/{report.id}")
    assert got.status_code == 200
    assert RunReport.model_validate(got.json()) == report
    assert client.get("/api/runs/missing").status_code == 404
    evs = client.get(f"/api/runs/{report.id}/events", params={"after": 4}).json()
    assert [e["seq"] for e in evs] == [5, 6, 7]
    assert [Event.model_validate(e) for e in client.get(f"/api/runs/{report.id}/events").json()] == events


def test_stream_replays_finished_run(client: TestClient):
    report = synthetic_report("feed0001")
    events = store(report)
    with client.stream("GET", f"/api/runs/{report.id}/stream") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in r.iter_lines() if line.startswith("data: ")]
    streamed = [Event.model_validate(json.loads(line[len("data: "):])) for line in lines]
    assert streamed == events
    assert streamed[-1].type == "done"
    assert RunReport.model_validate(streamed[-1].data["report"]) == report
    assert client.get("/api/runs/missing/stream").status_code == 404


def test_events_and_stream_honour_after(client: TestClient):
    report = synthetic_report("feed0002")
    events = store(report)
    assert [e["seq"] for e in client.get(f"/api/runs/{report.id}/events", params={"after": 5}).json()] == [6, 7]
    assert client.get(f"/api/runs/{report.id}/events", params={"after": 7}).json() == []
    assert stream_seqs(client, report.id, after=5) == events[5:]
    assert stream_seqs(client, report.id, after=7) == []
    assert stream_seqs(client, report.id) == events


def test_stream_follows_run_owned_by_another_process(client: TestClient, monkeypatch):
    """A run in the db but not in this process's memory is followed by polling until its done event lands."""
    monkeypatch.setattr(runner, "DB_POLL_SECONDS", 0.05)
    report = synthetic_report("feed0003")
    events = synthetic_events(report)
    live = report.model_copy(update={"status": "cloning", "finished_at": "", "results": [], "scores": [], "instances": []})
    db.upsert_run(live)
    for ev in events[:2]:
        db.insert_event(report.id, ev)
    assert not RunManager.get().is_live(report.id)

    def writer():
        for ev in events[2:]:
            time.sleep(0.15)
            db.insert_event(report.id, ev)
        db.upsert_run(report)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    streamed = stream_seqs(client, report.id)
    t.join(5)
    assert streamed == events  # every event exactly once, in order, ending with done
    assert streamed[-1].type == "done"
    # a reconnect that already has seq <= 4 gets only the rest, and the finished run streams without a poller
    assert stream_seqs(client, report.id, after=4) == events[4:]
    assert RunManager.get()._pollers == {}


def test_stream_stops_after_idle_limit_on_foreign_run(client: TestClient, monkeypatch):
    monkeypatch.setattr(runner, "DB_POLL_SECONDS", 0.05)
    monkeypatch.setattr(runner, "DB_POLL_IDLE_LIMIT", 0.3)
    report = synthetic_report("feed0004")
    report.status = "mining"
    db.upsert_run(report)
    db.insert_event(report.id, synthetic_events(report)[0])
    t0 = time.monotonic()
    streamed = stream_seqs(client, report.id)
    assert [e.seq for e in streamed] == [1]
    assert time.monotonic() - t0 < 5
    assert RunManager.get()._pollers == {}


def test_zero_candidates_finishes_without_prepare_env(manager: RunManager, monkeypatch):
    calls = stub_pipeline(monkeypatch, commits=321)
    seen: list[Event] = []
    run_id = manager.start_run("https://github.com/example/empty", ["claude-cli:haiku"], on_event=seen.append)
    assert manager.wait(run_id, timeout=20)
    report = manager.get_report(run_id)
    assert report.status == "done"
    assert report.scores == [] and report.instances == []
    assert report.stats.commits_scanned == 321 and report.stats.candidates == 0
    assert calls["prepare_env"] == 0
    types = [ev.type for ev in seen]
    assert types[-1] == "done" and "mine.done" in types and "scores" in types
    messages = [ev.data.get("message", "") for ev in seen if ev.type == "status"]
    assert any("no candidates matched the fix heuristic in 321 commits" in m and "Mined 321 commits, 0 candidates" in m for m in messages)
    assert db.get_run(run_id).status == "done"
    assert db.last_event(run_id).type == "done"
    # a late subscriber to the finished run replays everything after `after` then gets the sentinel
    q = manager.subscribe(run_id, after=seen[-2].seq)
    assert q.get(timeout=1).type == "done" and q.get(timeout=1) is None


def test_zero_candidates_with_plain_sink(manager: RunManager, monkeypatch):
    calls = stub_pipeline(monkeypatch, commits=7)
    sink = EventSink()
    report = RunReport(id="run00001", repo_url="https://github.com/example/empty", created_at=runner.now_iso(), model_ids=["claude-cli:haiku"])
    out = runner.execute_run(report, sink, ["claude-cli:haiku"], 5)
    assert out is report and report.scores == [] and report.repo_name == "example__empty"
    assert calls["prepare_env"] == 0
    assert [ev.type for ev in sink.events] == ["clone.done", "status", "mine.done", "status", "scores"]
    assert sink.events[-2].data["message"].startswith("no candidates matched the fix heuristic in 7 commits")


def test_repo_lock_serializes_runs_on_the_same_repo(manager: RunManager, monkeypatch):
    gate = threading.Event()
    entered = threading.Event()
    blocked_url = "https://github.com/example/shared"

    def clone_hook(repo_url: str) -> None:
        if repo_url == blocked_url:
            entered.set()
            assert gate.wait(10)

    stub_pipeline(monkeypatch, clone_hook=clone_hook)
    seen: dict[str, list[Event]] = {}

    def start(url: str) -> str:
        events: list[Event] = []
        run_id = manager.start_run(url, ["claude-cli:haiku"], on_event=events.append)
        seen[run_id] = events
        return run_id

    def waiting(run_id: str) -> bool:
        return any(ev.type == "status" and ev.data.get("message") == WAITING_FOR_REPO for ev in seen[run_id])

    first = start(blocked_url)
    assert entered.wait(5)
    second = start(blocked_url)
    wait_for(lambda: waiting(second), what="the second run to report it is waiting for the repo lock")
    assert manager.get_report(second).status == "queued"
    assert not any(ev.type == "clone.done" for ev in seen[second])
    other = start("https://github.com/example/other")
    assert manager.wait(other, timeout=10), "a run on another repo must not wait for this repo's lock"
    assert not waiting(other)
    assert not manager.wait(first, timeout=0.2) and not manager.wait(second, timeout=0.2)
    gate.set()
    assert manager.wait(first, timeout=10) and manager.wait(second, timeout=10)
    assert manager.get_report(first).status == "done" and manager.get_report(second).status == "done"
    assert not waiting(first)
    assert [ev.type for ev in seen[second]][:2] == ["status", "clone.done"]  # waited, then cloned
    lock = runner.repo_lock("example__shared")
    assert lock.acquire(blocking=False), "the repo lock must be released after the run"
    lock.release()


def test_recover_orphans_leaves_recent_runs_alone(manager: RunManager):
    now = datetime.now(timezone.utc)

    def stored(run_id: str, created: datetime, last_t: Optional[float]) -> RunReport:
        report = synthetic_report(run_id)
        report.status = "verifying"
        report.created_at = created.isoformat(timespec="seconds")
        db.upsert_run(report)
        if last_t is not None:
            db.insert_event(run_id, Event(seq=1, t=last_t, type="status", data={"status": "verifying"}))
        return report

    stored("fresh001", now - timedelta(minutes=2), 30.0)  # created 2 min ago: alive
    stored("busy0001", now - timedelta(hours=1), 3300.0)  # created an hour ago, last event 5 min ago: alive
    stored("dead0001", now - timedelta(hours=1), 60.0)  # last event 59 min ago: orphaned
    stored("dead0002", now - timedelta(minutes=20), None)  # no events at all, created 20 min ago: orphaned
    db.upsert_run(synthetic_report("done0001"))
    assert sorted(manager.recover_orphans()) == ["dead0001", "dead0002"]
    assert db.get_run("fresh001").status == "verifying" and db.get_run("busy0001").status == "verifying"
    for rid in ("dead0001", "dead0002"):
        assert db.get_run(rid).status == "failed"
        assert db.last_event(rid).type == "failed"
    assert db.get_run("done0001").status == "done"
    assert manager.recover_orphans() == []


def test_finish_is_exception_safe(manager: RunManager, monkeypatch):
    stub_pipeline(monkeypatch)
    real_insert = db.insert_event

    def flaky_insert(run_id: str, event: Event) -> None:
        if event.type in ("done", "failed"):
            raise RuntimeError("disk full")
        real_insert(run_id, event)

    monkeypatch.setattr(runner.db, "insert_event", flaky_insert)
    run_id = manager.start_run("https://github.com/example/flaky", ["claude-cli:haiku"])
    q = manager.subscribe(run_id)
    assert manager.wait(run_id, timeout=20)
    report = manager.get_report(run_id)
    assert report.status == "failed" and "disk full" in (report.error or "")
    assert db.get_run(run_id).status == "failed"
    items = []
    while True:
        item = q.get(timeout=2)
        items.append(item)
        if item is None:
            break
    assert all(isinstance(x, Event) for x in items[:-1])
    late = manager.subscribe(run_id)
    assert late.get_nowait() is not None and late.queue[-1] is None  # a late subscriber gets the sentinel immediately


def test_db_uses_primary_key_for_event_reads(manager: RunManager):
    conn = db._connection()
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT seq, t, type, data_json FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", ("x", 3)).fetchall()
    assert any("sqlite_autoindex_events_1" in row[-1] and "seq>?" in row[-1] for row in plan), plan
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.insert_event("uni00001", Event(seq=1, t=0.0, type="status", data={"message": "h\u00e9llo \u2014 \u4e16\u754c"}))
    raw = conn.execute("SELECT data_json FROM events WHERE run_id = 'uni00001'").fetchone()[0]
    assert "\u4e16\u754c" in raw and "\\u" not in raw
    assert db.get_events("uni00001")[0].data["message"] == "h\u00e9llo \u2014 \u4e16\u754c"


def test_start_run_rejects_bad_url(client: TestClient):
    r = client.post("/api/runs", json={"repo_url": "not a repo"})
    assert r.status_code == 400
    assert client.post("/api/runs", json={}).status_code == 422


def test_spa_fallback(client: TestClient):
    from rewind import server

    if not server.DIST_DIR.is_dir():
        pytest.skip("frontend/dist not built")
    index = client.get("/")
    assert index.status_code == 200 and "<div id=\"root\"" in index.text
    print_page = client.get("/print?run=abc")
    assert print_page.status_code == 200 and print_page.text == index.text
    assert client.get("/api/nope").status_code == 404
