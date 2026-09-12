"""Miner: clone a GitHub repo and walk its history for commits that look like real bug fixes with tests.

A candidate touches a small number of test files and a small number of source files and has a
commit message that reads like a fix. The verifier decides whether it is actually reconstructible.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import MAX_COMMITS_SCANNED, WORK_DIR
from .events import EventSink
from .models import Candidate

_TEST_PATH_RE = re.compile(r"(^|/)(tests?|testing)/|(^|/)(test_[^/]*\.py|[^/]*_test\.py|conftest\.py)$")
_FIX_RE = re.compile(
    r"\b(fix|fixes|fixed|fixing|bug|bugs|regression|incorrect|wrong|crash|crashes|traceback|error|errors|broken)\b"
    r"|\b(issue|closes|close|resolves|resolve|fixes|fix)\s*#?\d+",
    re.I,
)
_NOT_FIX_RE = re.compile(
    r"\b(typos?|docs?|docstrings?|lint|linting|flake8|black|isort|format|formatting|formatter|refactor|refactoring"
    r"|bump|bumps|release|version|changelog|chore|readme|pre-commit)\b|\b(doc|ci)\s*:",
    re.I,
)
_OTHER_PY_RE = re.compile(r"(^|/)docs?/|(^|/)(setup|noxfile|tasks|conftest_docs)\.py$")
_GITHUB_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_SHORT_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$")

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "PYTHONIOENCODING": "utf-8"}


@dataclass
class RepoCheckout:
    url: str
    name: str  # owner__repo
    path: Path
    default_branch: str
    head_sha: str


def git(path: Path, *args: str, check: bool = True, timeout: int = 900) -> str:
    """Run git in `path` and return stdout. Raises RuntimeError on failure when check=True.

    Output is decoded as UTF-8 explicitly (the Windows default is cp1252, which turns non-ASCII commit
    subjects and diffs into mojibake) and core.quotepath is off so non-ASCII paths come back verbatim.
    """
    res = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, **GIT_ENV},
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({res.returncode}): {res.stderr.strip()[-2000:]}")
    return res.stdout


def parse_repo_url(repo_url: str) -> tuple[str, str, str]:
    """Return (owner, repo, https clone url) for the accepted url forms."""
    s = repo_url.strip()
    m = _GITHUB_RE.match(s) or _SHORT_RE.match(s)
    if not m:
        raise ValueError(f"not a GitHub repo reference: {repo_url!r}")
    owner, repo = m.group(1), m.group(2)
    return owner, repo, f"https://github.com/{owner}/{repo}.git"


def is_test_path(path: str) -> bool:
    """Same rule scoring uses: (^|/)tests?/, (^|/)testing/, test_*.py, *_test.py, conftest.py."""
    return bool(_TEST_PATH_RE.search(path.replace("\\", "/")))


def _emit(sink: Optional[EventSink], type: str, **data) -> None:
    if sink is not None:
        sink.emit(type, **data)


def _default_branch(path: Path) -> str:
    out = git(path, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).strip()
    if not out:
        git(path, "remote", "set-head", "origin", "-a")
        out = git(path, "symbolic-ref", "-q", "refs/remotes/origin/HEAD").strip()
    return out.rsplit("/", 1)[-1]


def clone_repo(repo_url: str, work_dir: Path = WORK_DIR, sink: Optional[EventSink] = None) -> RepoCheckout:
    """Full-history clone cached at work_dir/repos/<owner>__<repo>; refreshed to origin's default branch."""
    owner, repo, url = parse_repo_url(repo_url)
    name = f"{owner}__{repo}"
    path = Path(work_dir) / "repos" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if (path / ".git").is_dir():
        _emit(sink, "status", status="cloning", message=f"updating cached clone of {owner}/{repo}")
        git(path, "fetch", "-q", "--prune", "origin")
    else:
        _emit(sink, "status", status="cloning", message=f"cloning {owner}/{repo}")
        git(path.parent, "clone", "-q", "--no-checkout", url, str(path))
    git(path, "config", "core.autocrlf", "false")
    git(path, "config", "core.longpaths", "true")
    git(path, "config", "advice.detachedHead", "false")
    branch = _default_branch(path)
    git(path, "checkout", "-q", "--force", "-B", branch, f"origin/{branch}")
    git(path, "clean", "-fdq")
    head = git(path, "rev-parse", "HEAD").strip()
    commits = int(git(path, "rev-list", "--count", "HEAD").strip() or 0)
    _emit(sink, "clone.done", repo_name=name, commits=commits)
    return RepoCheckout(url=url, name=name, path=path, default_branch=branch, head_sha=head)


def _name_status(repo: RepoCheckout, sha: str) -> list[tuple[str, str]]:
    """(status letter, path) for every file changed by `sha` (renames report the new path)."""
    out = git(repo.path, "show", "--no-color", "--format=", "--name-status", "-M", sha)
    return _parse_name_status(out.splitlines())


def _parse_name_status(lines: list[str]) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for line in lines:
        if not line.strip() or "\t" not in line:
            continue
        parts = line.split("\t")
        status = parts[0][0]
        files.append((status, parts[-1].strip()))
    return files


def classify_files(paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split changed paths into (test .py files, source .py files, everything else)."""
    tests: list[str] = []
    source: list[str] = []
    other: list[str] = []
    for p in paths:
        p = p.replace("\\", "/")
        if not p.endswith(".py") or _OTHER_PY_RE.search(p):
            other.append(p)
        elif is_test_path(p):
            tests.append(p)
        else:
            source.append(p)
    return tests, source, other


def commit_files(repo: RepoCheckout, sha: str) -> tuple[list[str], list[str], list[str]]:
    """(test_files, source_files, other) touched by a commit."""
    return classify_files([p for _, p in _name_status(repo, sha)])


def looks_like_fix(subject: str, body: str) -> bool:
    """Bug-fix wording in the subject line (body-only mentions admit feature commits), minus housekeeping words."""
    if not _FIX_RE.search(subject):
        return False
    return not _NOT_FIX_RE.search(subject)


def _is_candidate(tests: list[str], source: list[str]) -> bool:
    real_tests = [t for t in tests if not t.endswith("conftest.py")]
    return 1 <= len(real_tests) <= 2 and 1 <= len(source) <= 3


def _log_records(repo: RepoCheckout, max_commits: int) -> list[dict]:
    """One dict per commit from a single `git log --name-status` walk, newest first."""
    fmt = "%x1e%H%x00%P%x00%aI%x00%s%x00%B%x00"
    out = git(repo.path, "log", "--no-merges", "--no-color", f"-n{max_commits}", f"--format={fmt}", "--name-status", "-M", repo.default_branch)
    records: list[dict] = []
    for rec in out.split("\x1e"):
        if not rec.strip():
            continue
        sha, parents, date, subject, body, rest = rec.split("\x00", 5)
        files = _parse_name_status(rest.splitlines())
        records.append({"sha": sha, "parents": parents.split(), "date": date, "subject": subject.strip(), "message": body.strip(), "files": files})
    return records


def find_candidates(repo: RepoCheckout, max_commits: int = MAX_COMMITS_SCANNED, sink: Optional[EventSink] = None) -> list[Candidate]:
    """Walk the default branch newest-first and keep commits that look like small, tested bug fixes."""
    _emit(sink, "status", status="mining", message=f"scanning up to {max_commits} commits")
    found: list[Candidate] = []
    for rec in _log_records(repo, max_commits):
        tests, source, _other = classify_files([p for _, p in rec["files"]])
        ok = len(rec["parents"]) == 1 and _is_candidate(tests, source) and looks_like_fix(rec["subject"], rec["message"])
        _emit(sink, "mine.scan", sha=rec["sha"], subject=rec["subject"], candidate=ok)
        if not ok:
            continue
        cand = Candidate(
            sha=rec["sha"],
            parent_sha=rec["parents"][0],
            subject=rec["subject"],
            message=rec["message"],
            author_date=rec["date"],
            test_files=tests,
            source_files=source,
        )
        found.append(cand)
        _emit(sink, "mine.candidate", sha=cand.sha, subject=cand.subject, test_files=tests, source_files=source)
    return found


def scanned_count(repo: RepoCheckout, max_commits: int = MAX_COMMITS_SCANNED) -> int:
    """How many commits `find_candidates` walks for the same limit."""
    out = git(repo.path, "rev-list", "--no-merges", f"-n{max_commits}", repo.default_branch)
    return len(out.split())
