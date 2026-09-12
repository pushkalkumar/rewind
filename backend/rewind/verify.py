"""Verifier: turn a Candidate into an Instance by construction, or discard it with a reason.

Broken state = parent commit + only the fix commit's test files. The target tests must fail there
and pass at the fix commit. The broken tree is exported (no .git) for sandboxes to copy.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import MAX_COMMITS_SCANNED, MAX_INSTANCES, TEST_TIMEOUT, WORK_DIR
from .events import EventSink, PrintSink
from .gitmine import RepoCheckout, clone_repo, find_candidates, git, is_test_path, scanned_count
from .models import Candidate, DiscardedCandidate, Instance, MinedStats, PytestRun
from .pytest_runner import run_pytest
from .sandbox import RunResult

TREE_SKIP = {".git", "__pycache__", ".pytest_cache", ".tox", ".venv", "venv", ".mypy_cache", ".hypothesis", ".nox"}
EXTRAS = ["dev", "test", "tests", "testing", ""]
DEP_GROUPS = ["tests", "test", "testing", "dev"]
REQ_GLOBS = ["requirements-dev.txt", "requirements/test*.txt", "requirements-test*.txt", "tests/requirements.txt", "requirements/dev*.txt"]
_TRAILER_RE = re.compile(r"^\s*(co-authored-by|signed-off-by)\s*:", re.I)
FULL_SUITE_CAP = 90


@dataclass
class RepoEnv:
    venv_python: Path
    install_log: str


def _emit(sink: Optional[EventSink], type: str, **data) -> None:
    if sink is not None:
        sink.emit(type, **data)


def _rmtree(path: Path) -> None:
    def onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=onerror)


# -- environment -----------------------------------------------------------

def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _pip(python: Path, args: list[str], cwd: Path, timeout: int = 900) -> tuple[bool, str]:
    """Quiet pip invocation; returns (ok, log chunk)."""
    cmd = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", *args]
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONIOENCODING": "utf-8", "PIP_NO_INPUT": "1"}
    try:
        res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
        out = (res.stdout + res.stderr).strip()
        ok = res.returncode == 0 and "does not provide the extra" not in out
        return ok, f"$ pip install {' '.join(args)} -> {'ok' if ok else 'FAILED'}\n{out[-3000:]}\n"
    except subprocess.TimeoutExpired:
        return False, f"$ pip install {' '.join(args)} -> TIMEOUT after {timeout}s\n"


def _has_pytest(python: Path) -> bool:
    if not python.exists():
        return False
    res = subprocess.run([str(python), "-c", "import pytest"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return res.returncode == 0


def _dependency_groups(repo_path: Path) -> list[str]:
    """PEP 735 group names declared in pyproject.toml (pip >= 25.1 installs them with --group)."""
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.is_file():
        return []
    groups: list[str] = []
    in_section = False
    for line in pyproject.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped == "[dependency-groups]"
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*=", stripped) if in_section else None
        if m:
            groups.append(m.group(1).strip('"'))
    return groups


def _tox_deps(repo_path: Path) -> list[str]:
    """Plain requirement lines from tox.ini [testenv] deps (skips tox substitutions)."""
    tox = repo_path / "tox.ini"
    if not tox.is_file():
        return []
    deps: list[str] = []
    section = ""
    in_deps = False
    for line in tox.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("["):
            section, in_deps = line.strip(), False
            continue
        if section != "[testenv]":
            continue
        if re.match(r"^deps\s*=", line):
            in_deps = True
            rest = line.split("=", 1)[1].strip()
            if rest:
                deps.append(rest)
            continue
        if in_deps and line[:1].isspace() and line.strip():
            deps.append(line.strip())
        elif in_deps and not line[:1].isspace():
            in_deps = False
    out: list[str] = []
    for d in deps:
        if "{" in d or d.startswith("#"):
            continue
        if d.startswith("-r"):
            out.extend(d.split())
        else:
            out.append(d)
    return out


def prepare_env(repo: RepoCheckout, sink: Optional[EventSink] = None) -> RepoEnv:
    """Create (or reuse) a venv with the repo installed editable plus its test dependencies."""
    venv = Path(WORK_DIR) / "venvs" / repo.name
    python = _venv_python(venv)
    marker = venv / ".rewind_ready"
    if marker.is_file() and _has_pytest(python):
        _emit(sink, "status", status="verifying", message=f"reusing venv for {repo.name}")
        return RepoEnv(venv_python=python, install_log="reused existing venv\n")
    log: list[str] = []
    _emit(sink, "status", status="verifying", message="creating venv")
    _rmtree(venv)
    venv.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok, chunk = _pip(python, ["--upgrade", "pip"], repo.path)
    log.append(chunk)
    _emit(sink, "status", status="verifying", message="installing deps (editable install)")
    for extra in EXTRAS:
        target = f".[{extra}]" if extra else "."
        ok, chunk = _pip(python, ["-e", target], repo.path)
        log.append(chunk)
        if ok:
            break
    # tzdata: Windows has no system tz database, and zoneinfo-based tests need one everywhere.
    _, chunk = _pip(python, ["pytest", "tzdata"], repo.path)
    log.append(chunk)
    declared = _dependency_groups(repo.path)
    for group in [g for g in DEP_GROUPS if g in declared]:
        _emit(sink, "status", status="verifying", message=f"installing dependency group '{group}'")
        ok, chunk = _pip(python, ["--group", group], repo.path)
        log.append(chunk)
        if ok and group != "dev":
            break
    req_files = [p for g in REQ_GLOBS for p in sorted(repo.path.glob(g))]
    for req in req_files:
        _emit(sink, "status", status="verifying", message=f"installing {req.relative_to(repo.path).as_posix()}")
        _, chunk = _pip(python, ["-r", str(req)], repo.path)
        log.append(chunk)
    tox_deps = _tox_deps(repo.path)
    if tox_deps and not req_files:
        _emit(sink, "status", status="verifying", message="installing tox [testenv] deps")
        _, chunk = _pip(python, tox_deps, repo.path)
        log.append(chunk)
    extra_req = Path(WORK_DIR) / ".rewind_extra_requirements"
    if extra_req.is_file():
        _emit(sink, "status", status="verifying", message="installing .rewind_extra_requirements")
        _, chunk = _pip(python, ["-r", str(extra_req)], repo.path)
        log.append(chunk)
    if not _has_pytest(python):
        raise RuntimeError(f"could not set up a venv with pytest for {repo.name}:\n" + "".join(log)[-3000:])
    marker.write_text("ok\n")
    return RepoEnv(venv_python=python, install_log="".join(log))


# -- git state helpers -----------------------------------------------------

def _reset_to(repo: RepoCheckout, ref: str) -> None:
    git(repo.path, "checkout", "-q", "--force", ref)
    git(repo.path, "clean", "-fdq")


def _test_patch_files(repo: RepoCheckout, cand: Candidate) -> list[str]:
    """Test .py files plus any non-.py files under test dirs (fixtures) the fix commit touched."""
    out = git(repo.path, "show", "--no-color", "--format=", "--name-only", "-M", cand.sha)
    changed = [l.strip().replace("\\", "/") for l in out.splitlines() if l.strip()]
    extra = [p for p in changed if p not in cand.test_files and not p.endswith(".py") and is_test_path(p)]
    return list(cand.test_files) + extra


def _existing_at(repo: RepoCheckout, sha: str, files: list[str]) -> list[str]:
    if not files:
        return []
    out = git(repo.path, "ls-tree", "-r", "--name-only", sha, "--", *files)
    return [l.strip() for l in out.splitlines() if l.strip()]


def _stale_test_paths(repo: RepoCheckout, cand: Candidate) -> list[str]:
    """Test paths the fix commit deleted or renamed away (their OLD names), which the parent tree still has.

    `--name-status -M` reports a rename as `R<score>\\told\\tnew` and a delete as `D\\tpath`; bringing only the new
    names forward would leave the old module sitting next to them in the broken tree.
    """
    out = git(repo.path, "diff", "--no-color", "--name-status", "-M", cand.parent_sha, cand.sha)
    stale: list[str] = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split("\t")]
        if len(parts) < 2 or not parts[0]:
            continue
        status, old = parts[0][0], parts[1].replace("\\", "/")
        if status in ("R", "D") and is_test_path(old) and old not in stale:
            stale.append(old)
    return stale


def _remove_paths(repo: RepoCheckout, paths: list[str]) -> None:
    if not paths:
        return
    git(repo.path, "rm", "-q", "--cached", "--ignore-unmatch", "--", *paths)
    for f in paths:
        p = repo.path / f
        if p.is_file():
            p.unlink()


def _build_broken_state(repo: RepoCheckout, cand: Candidate, patch_files: list[str], stale: Optional[list[str]] = None) -> None:
    """Parent commit with exactly the fix commit's test files: new/changed ones brought forward, deleted and
    renamed-away ones removed."""
    _reset_to(repo, cand.parent_sha)
    present = _existing_at(repo, cand.sha, patch_files)
    if present:
        git(repo.path, "checkout", cand.sha, "--", *present)
    deleted = [f for f in patch_files if f not in present]
    if stale is None:
        stale = _stale_test_paths(repo, cand)
    _remove_paths(repo, deleted + [f for f in stale if f not in deleted])


def _make_runner(repo: RepoCheckout, env: RepoEnv):
    """A `run(cmd, timeout) -> RunResult` closure over the repo checkout and its venv."""
    src = repo.path / "src"

    def run(cmd: list[str], timeout: int = TEST_TIMEOUT, extra_env: Optional[dict[str, str]] = None) -> RunResult:
        cmd = list(cmd)
        if cmd and cmd[0] == "python":
            cmd[0] = str(env.venv_python)
        pypath = os.pathsep.join([str(repo.path)] + ([str(src)] if src.is_dir() else []))
        full_env = {
            **os.environ,
            "PYTHONPATH": pypath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PY_COLORS": "0",
            "NO_COLOR": "1",
            **(extra_env or {}),
        }
        t0 = time.time()
        try:
            res = subprocess.run(cmd, cwd=str(repo.path), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=full_env)
            return RunResult(res.returncode, res.stdout, res.stderr, False, round(time.time() - t0, 2))
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return RunResult(-1, out, err + f"\n[timed out after {timeout}s]", True, round(time.time() - t0, 2))

    return run


# -- verification ----------------------------------------------------------

def _existing_targets(repo: RepoCheckout, files: list[str]) -> list[str]:
    return [f for f in files if (repo.path / f).is_file()]


def _in_files(nodeids: set[str], files: list[str]) -> set[str]:
    wanted = {f.replace("\\", "/") for f in files}
    return {n for n in nodeids if n.split("::", 1)[0].replace("\\", "/") in wanted}


def _no_tests_collected(run: PytestRun) -> bool:
    return run.returncode != 0 and not run.tests


def _failed_under(nodeid: str, not_passed: set[str]) -> bool:
    """True when `nodeid` itself, or a scope above it (its module/class errored at collection), did not pass."""
    return nodeid in not_passed or any(nodeid.startswith(n + "::") for n in not_passed)


def _stable_baseline(full_broken: PytestRun, full_fixed: PytestRun, broken: PytestRun, test_files: list[str]) -> set[str]:
    """Node ids that pass both without and with the fix: the regression baseline.

    Requiring both observations drops ids whose parametrize values are not reproducible (built from the clock or
    a random seed), which the agent's regression check could never match again, and tests the gold patch itself
    breaks, which are not the agent's fault. If the fix-commit full run is unusable, fall back to the single broken
    run but still require target-file ids to be reproducible across the two broken-state runs.
    """
    if not full_fixed.timed_out and full_fixed.passed:
        return full_broken.passed & full_fixed.passed
    wanted = {f.replace("\\", "/") for f in test_files}
    return {n for n in full_broken.passed if n.split("::", 1)[0].replace("\\", "/") not in wanted or n in broken.passed}


def issue_text_from(message: str) -> str:
    """The commit's subject line only.

    Commit bodies routinely narrate the fix ("use dict access instead of getattr"), which would hand the agent the
    answer. The subject reads like a bug title; the failing tests the agent gets alongside it are the spec.
    """
    for line in message.splitlines():
        if line.strip() and not _TRAILER_RE.match(line):
            return line.strip()
    return message.strip()


def _export_tree(repo: RepoCheckout, dest: Path) -> None:
    _rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo.path, dest, ignore=shutil.ignore_patterns(*TREE_SKIP, "*.egg-info", "*.pyc"))


def _discard(cand: Candidate, reason: str, sink: Optional[EventSink]) -> DiscardedCandidate:
    _emit(sink, "verify.discard", sha=cand.sha, subject=cand.subject, reason=reason)
    return DiscardedCandidate(sha=cand.sha, subject=cand.subject, reason=reason)


def verify_candidate(
    repo: RepoCheckout,
    env: RepoEnv,
    cand: Candidate,
    trees_dir: Path,
    sink: Optional[EventSink] = None,
    timeout: int = TEST_TIMEOUT,
) -> Instance | DiscardedCandidate:
    """Reconstruct the broken state, prove the target tests fail there and pass at the fix, export the tree."""
    t0 = time.time()
    _emit(sink, "verify.start", sha=cand.sha, subject=cand.subject)
    run = _make_runner(repo, env)
    patch_files = _test_patch_files(repo, cand)
    try:
        stale = _stale_test_paths(repo, cand)
        _build_broken_state(repo, cand, patch_files, stale)
        targets = _existing_targets(repo, cand.test_files)
        if not targets:
            return _discard(cand, "no tests collected", sink)
        broken = run_pytest(run, targets, timeout=timeout)
        if broken.timed_out:
            return _discard(cand, "timeout", sink)
        if _no_tests_collected(broken):
            return _discard(cand, "no tests collected", sink)

        _reset_to(repo, cand.sha)
        fixed = run_pytest(run, targets, timeout=timeout)
        if fixed.timed_out:
            return _discard(cand, "timeout", sink)

        broken_passed = _in_files(broken.passed, cand.test_files)
        broken_not_passed = _in_files(broken.not_passed, cand.test_files)
        fixed_passed = _in_files(fixed.passed, cand.test_files)
        # fail-to-pass = seen failing (or under a collection error) without the fix, passing with it. Ids that only
        # exist in the fixed run (clock-based parametrize ids) are not evidence of anything.
        fail_to_pass = sorted(n for n in fixed_passed if _failed_under(n, broken_not_passed))
        pass_to_pass_touched = sorted(fixed_passed & broken_passed)
        if not broken_not_passed:
            return _discard(cand, "tests pass without the fix", sink)
        if not fail_to_pass:
            if _in_files(fixed.not_passed, cand.test_files):
                return _discard(cand, "tests fail at fix commit", sink)
            return _discard(cand, "no fail-to-pass tests", sink)

        full_fixed = run_pytest(run, [], timeout=min(timeout, FULL_SUITE_CAP))  # still at the fix commit
        _build_broken_state(repo, cand, patch_files, stale)
        full = run_pytest(run, [], timeout=min(timeout, FULL_SUITE_CAP))
        if full.timed_out or not full.passed:
            pass_to_pass, regression_targets = pass_to_pass_touched, list(targets)
        else:
            pass_to_pass, regression_targets = sorted(_stable_baseline(full, full_fixed, broken, cand.test_files) - set(fail_to_pass)), []

        tree = Path(trees_dir) / cand.sha[:10]
        _export_tree(repo, tree)

        gold_patch = git(repo.path, "diff", "--no-color", "--no-ext-diff", cand.parent_sha, cand.sha, "--", *cand.source_files)
        test_patch = git(repo.path, "diff", "--no-color", "--no-ext-diff", cand.parent_sha, cand.sha, "--", *patch_files, *[f for f in stale if f not in patch_files])
        seconds = round(time.time() - t0, 2)
        inst = Instance(
            id=cand.sha[:10],
            candidate=cand,
            issue_text=issue_text_from(cand.message),
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            regression_targets=regression_targets,
            test_patch=test_patch,
            gold_patch=gold_patch,
            test_files=targets,  # only test files that exist at the fix commit, so the agent's default run_tests hits real files
            verify_seconds=seconds,
        )
        _emit(sink, "verify.keep", sha=cand.sha, subject=cand.subject, fail_to_pass=fail_to_pass, pass_to_pass_count=len(pass_to_pass), seconds=seconds)
        return inst
    except Exception as e:  # git or filesystem trouble on this commit: discard, keep going
        return _discard(cand, f"error: {str(e)[:200]}", sink)
    finally:
        try:
            _reset_to(repo, repo.default_branch)
        except Exception:
            pass


def build_instances(
    repo: RepoCheckout,
    env: RepoEnv,
    candidates: list[Candidate],
    max_instances: int = MAX_INSTANCES,
    trees_dir: Optional[Path] = None,
    sink: Optional[EventSink] = None,
    commits_scanned: int = 0,
    timeout: int = TEST_TIMEOUT,
) -> tuple[list[Instance], MinedStats]:
    """Verify candidates in order until `max_instances` survive; returns the instances and mining stats."""
    trees_dir = Path(trees_dir) if trees_dir else Path(WORK_DIR) / "trees" / repo.name
    stats = MinedStats(commits_scanned=commits_scanned, candidates=len(candidates))
    kept: list[Instance] = []
    _emit(sink, "status", status="verifying", message=f"verifying {len(candidates)} candidates")
    for cand in candidates:
        if len(kept) >= max_instances:
            break
        result = verify_candidate(repo, env, cand, trees_dir, sink=sink, timeout=timeout)
        if isinstance(result, Instance):
            kept.append(result)
        else:
            stats.discarded_list.append(result)
            stats.discard_reasons[result.reason] = stats.discard_reasons.get(result.reason, 0) + 1
    stats.verified = len(kept)
    stats.discarded = len(stats.discarded_list)
    stats.benchmarked = len(kept)
    _emit(sink, "mine.done", stats=stats.model_dump())
    return kept, stats


# -- CLI -------------------------------------------------------------------

def _stats_line(stats: MinedStats) -> str:
    reasons = ", ".join(f"{k}: {v}" for k, v in sorted(stats.discard_reasons.items(), key=lambda kv: -kv[1]))
    return f"Mined {stats.commits_scanned} commits, {stats.candidates} candidates, {stats.verified} verified, {stats.discarded} discarded ({reasons})"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rewind.verify", description="Mine and verify bug-fix instances from a GitHub repo.")
    ap.add_argument("repo")
    ap.add_argument("--max", type=int, default=MAX_INSTANCES, help="stop after this many verified instances")
    ap.add_argument("--commits", type=int, default=MAX_COMMITS_SCANNED, help="commits to scan")
    ap.add_argument("--timeout", type=int, default=TEST_TIMEOUT, help="per pytest run timeout (s)")
    args = ap.parse_args(argv)
    t0 = time.time()
    sink = PrintSink()
    repo = clone_repo(args.repo, sink=sink)
    cands = find_candidates(repo, args.commits, sink=sink)
    env = prepare_env(repo, sink=sink)
    kept, stats = build_instances(repo, env, cands, max_instances=args.max, sink=sink, commits_scanned=scanned_count(repo, args.commits), timeout=args.timeout)
    print()
    print(f"{'id':<11} {'subject':<58} {'F2P':>4} {'P2P':>5} {'secs':>7}")
    for inst in kept:
        print(f"{inst.id:<11} {inst.candidate.subject[:58]:<58} {len(inst.fail_to_pass):>4} {len(inst.pass_to_pass):>5} {inst.verify_seconds:>7.1f}")
    print(_stats_line(stats))
    print(f"Total wall time: {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
