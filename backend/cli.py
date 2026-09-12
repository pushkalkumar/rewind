"""Rewind command line. Run from backend/:  python cli.py <command> ...

  mine <repo> [--max N] [--commits M]        clone, mine and verify; print the instance table
  bench <repo> [--models a,b] [--max N]      full run in-process; prints the report card at the end
  serve [--port 8000] [--host 127.0.0.1]     start the API + frontend server
  export <run_id> [--out path]               write {"report", "events"} JSON (the frontend demo fixture)
  runs                                       list stored runs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rewind import db  # noqa: E402
from rewind.config import MAX_COMMITS_SCANNED, MAX_INSTANCES, ROOT  # noqa: E402
from rewind.models import Event, RunReport  # noqa: E402
from rewind.runner import RunManager, stats_line  # noqa: E402
from rewind.verify import main as verify_main  # noqa: E402

DEFAULT_EXPORT = Path(ROOT) / "frontend" / "src" / "demo" / "run.json"


def print_event(ev: Event) -> None:
    data = ev.data
    if ev.type == "done":
        data = {"status": data.get("report", {}).get("status")}
    elif ev.type == "bench.result":
        r = data.get("result", {})
        data = {k: r.get(k) for k in ("instance_id", "model_id", "fixed", "cheated", "broke", "steps_used", "error")}
    elif ev.type == "mine.scan":
        if not data.get("candidate"):
            return
    print(f"[{ev.t:7.2f}s] {ev.type:<16} {json.dumps(data, ensure_ascii=False, default=str)[:220]}", flush=True)


def flags(fixed: bool, cheated: bool, broke: bool) -> str:
    return " ".join(("FIXED" if fixed else "fixed", "CHEATED" if cheated else "cheated", "BROKE" if broke else "broke"))


def report_card(report: RunReport) -> str:
    lines = [f"=== Rewind report {report.id} - {report.repo_name or report.repo_url} - {report.status} ===", stats_line(report.stats)]
    if report.error:
        lines.append(f"error: {report.error}")
    lines.append("")
    lines.append(f"{'model':<28} {'grade':>5} {'score':>6} {'fixed':>6} {'cheated':>8} {'broke':>6} {'honest':>7}")
    for s in report.scores:
        lines.append(f"{s.model_label[:28]:<28} {s.grade:>5} {s.score:>6.2f} {s.fixed:>3}/{s.n:<2} {s.cheated:>5}/{s.n:<2} {s.broke:>3}/{s.n:<2} {s.honest:>4}/{s.n:<2}")
    lines.append("")
    lines.append(f"{'instance':<11} {'model':<20} {'flags':<21} {'steps':>5} {'secs':>7}  subject")
    subjects = {inst.id: inst.candidate.subject for inst in report.instances}
    for r in report.results:
        err = f"  [error: {r.error[:60]}]" if r.error else ""
        lines.append(f"{r.instance_id:<11} {r.model_label[:20]:<20} {flags(r.fixed, r.cheated, r.broke):<21} {r.steps_used:>5} {r.seconds:>7.1f}  {subjects.get(r.instance_id, '')[:50]}{err}")
    return "\n".join(lines)


def cmd_mine(args: argparse.Namespace) -> int:
    argv = [args.repo, "--max", str(args.max), "--commits", str(args.commits)]
    return verify_main(argv)


def cmd_bench(args: argparse.Namespace) -> int:
    models = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else None
    mgr = RunManager.get()
    run_id = mgr.start_run(args.repo, models, args.max, on_event=print_event)
    print(f"run id: {run_id}", flush=True)
    try:
        # Poll with a timeout: a bare Event.wait() cannot be interrupted by Ctrl+C on Windows.
        while not mgr.wait(run_id, timeout=0.5):
            pass
    except KeyboardInterrupt:
        print("interrupted; the run thread keeps going until the process exits", file=sys.stderr)
        return 130
    report = mgr.get_report(run_id)
    print()
    print(report_card(report))
    return 0 if report.status == "done" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("rewind.server:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    report = db.get_run(args.run_id)
    if report is None:
        print(f"run {args.run_id!r} not found", file=sys.stderr)
        return 1
    events = db.get_events(args.run_id, 0)
    out = Path(args.out) if args.out else DEFAULT_EXPORT
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"report": report.model_dump(), "events": [ev.model_dump() for ev in events]}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes; {len(events)} events, status {report.status})")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    rows = db.list_runs()
    if not rows:
        print("no runs yet")
        return 0
    print(f"{'id':<10} {'status':<13} {'created_at':<26} repo")
    for r in rows:
        print(f"{r['id']:<10} {r['status']:<13} {r['created_at']:<26} {r['repo_url']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rewind", description="A benchmark a repository generates about itself.")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("mine", help="clone, mine and verify instances")
    p.add_argument("repo")
    p.add_argument("--max", type=int, default=MAX_INSTANCES)
    p.add_argument("--commits", type=int, default=MAX_COMMITS_SCANNED)
    p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("bench", help="full run: mine, verify, benchmark, score")
    p.add_argument("repo")
    p.add_argument("--models", default=None, help="comma-separated model ids (default: config.default_models())")
    p.add_argument("--max", type=int, default=None, help="max verified instances")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("serve", help="start the API server")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("export", help="export a run as {report, events} JSON")
    p.add_argument("run_id")
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("runs", help="list stored runs")
    p.set_defaults(fn=cmd_runs)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
