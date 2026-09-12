"""Run pytest through any `run(cmd, timeout) -> RunResult` callable and parse real node ids from `-rA` output.

Works identically on the host (verifier) and inside a Sandbox (agent), because both expose the same run() shape.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from .models import PytestRun, TestResult
from .sandbox import RunResult

# --continue-on-collection-errors: one module that fails to import must not abort the whole-suite
# baseline (an empty baseline would make BROKE impossible to detect); it shows up as an ERROR entry instead.
PYTEST_BASE = ["python", "-m", "pytest", "-p", "no:cacheprovider", "-rA", "-q", "--color=no", "--no-header", "--continue-on-collection-errors"]
# Node ids may contain spaces (parametrize ids), so the id is everything up to the ` - ` that separates it from
# the message; see _split_nodeid for why that separator is found bracket-aware rather than lazily.
_SUMMARY_RE = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(.+?)\s*$")
_SKIPPED_RE = re.compile(r"^(?:\[\d+\]\s+)?(.+?):\d+:\s*(.*)$")
_STATUS_MAP = {"PASSED": "passed", "XPASS": "passed", "FAILED": "failed", "XFAIL": "failed", "ERROR": "error", "SKIPPED": "skipped"}


def build_cmd(targets: list[str], node_ids: Optional[list[str]] = None, extra: Optional[list[str]] = None) -> list[str]:
    cmd = list(PYTEST_BASE)
    if node_ids:
        cmd += node_ids
    else:
        cmd += targets
    if extra:
        cmd += extra
    return cmd


def _split_nodeid(rest: str) -> tuple[str, str]:
    """Split `<nodeid> - <message>` on the first ` - ` that sits outside the id's parametrize brackets.

    A plain lazy split would cut `tests/t.py::test_p[c - d] - AssertionError: msg - x` inside the id, so the
    separator is the first ` - ` at which every `[` opened so far has been closed. No separator: no message
    (a bare `ERROR tests/t.py` collection error, or any PASSED line).
    """
    start = 0
    while True:
        i = rest.find(" - ", start)
        if i < 0:
            return rest, ""
        head = rest[:i]
        if head.count("[") == head.count("]"):
            return head, rest[i + 3 :].strip()
        start = i + 1


def parse_pytest_output(stdout: str) -> list[TestResult]:
    results: dict[str, TestResult] = {}
    in_summary = False
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if "short test summary info" in line:
            in_summary = True
            continue
        if not in_summary:
            continue
        if line.startswith("=") and ("passed" in line or "failed" in line or "error" in line or "no tests ran" in line or "skipped" in line or "warning" in line):
            break
        m = _SUMMARY_RE.match(line)
        if not m:
            continue
        kind, rest = m.group(1), m.group(2)
        if kind == "SKIPPED":
            # SKIPPED [1] tests/test_x.py:12: reason  -> normalise to the file (pytest prints no node id here)
            sm = _SKIPPED_RE.match(rest)
            nodeid, msg = (sm.group(1).replace("\\", "/"), sm.group(2)) if sm else (rest, "")
        else:
            nodeid, msg = _split_nodeid(rest)
        status = _STATUS_MAP[kind]
        prev = results.get(nodeid)
        # a nodeid can appear twice (e.g. setup ERROR + teardown); keep the worst
        if prev is None or (prev.status == "passed" and status != "passed"):
            results[nodeid] = TestResult(nodeid=nodeid, status=status, message=msg[:300])
    return list(results.values())


def run_pytest(run: Callable[..., RunResult], targets: list[str], node_ids: Optional[list[str]] = None, timeout: int = 120) -> PytestRun:
    cmd = build_cmd(targets, node_ids)
    t0 = time.time()
    res = run(cmd, timeout)
    tests = parse_pytest_output(res.stdout)
    tail = (res.stdout + "\n" + res.stderr)[-4000:]
    return PytestRun(returncode=res.returncode, tests=tests, stdout_tail=tail, timed_out=res.timed_out, seconds=round(time.time() - t0, 2))


def summarize(run: PytestRun, limit: int = 3000) -> str:
    """Human/agent-facing summary of a pytest run."""
    passed = run.by_status("passed")
    failed = [t for t in run.tests if t.status in ("failed", "error")]
    head = f"{len(passed)} passed, {len(failed)} failed/errored" + (" (TIMED OUT)" if run.timed_out else "")
    lines = [head]
    for t in failed[:20]:
        lines.append(f"FAILED {t.nodeid}" + (f" - {t.message}" if t.message else ""))
    body = "\n".join(lines)
    tail = run.stdout_tail
    if failed and tail:
        body += "\n--- pytest output (tail) ---\n" + tail[-(limit - len(body)) :]
    return body[:limit]
