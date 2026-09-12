"""Local sandbox: an isolated copy of the broken tree in its own directory, run with a prepared venv.

Isolation here is filesystem + fresh git baseline, not a container. Docker/Daytona backends give the
same behavior inside a container; this one is what runs on a bare laptop with no Docker.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from ..config import WORK_DIR
from . import RunResult, Sandbox, SandboxSpec

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".rewind", ".mypy_cache", ".tox", ".eggs"}
EXCLUDES = ["__pycache__/", "*.pyc", ".rewind*", ".pytest_cache/", "*.egg-info/", ".hypothesis/"]
GIT_ENV = {"GIT_AUTHOR_NAME": "rewind", "GIT_AUTHOR_EMAIL": "rewind@local", "GIT_COMMITTER_NAME": "rewind", "GIT_COMMITTER_EMAIL": "rewind@local"}


def _rmtree(path: Path) -> None:
    def onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=onerror)


class LocalSandbox(Sandbox):
    name = "local"

    def __init__(self, root: Path, python: Path):
        self.root = Path(root).resolve()  # absolute up front: a relative REWIND_WORK_DIR would break relative_to() in list_files
        self.python = python

    @classmethod
    def create(cls, spec: SandboxSpec) -> "LocalSandbox":
        root = WORK_DIR / "sandboxes" / uuid.uuid4().hex[:10]
        root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(spec.tree, root, ignore=shutil.ignore_patterns(*SKIP_DIRS))
        python = Path(spec.venv_python) if spec.venv_python else Path(sys.executable)
        sb = cls(root, python)
        sb._git("init", "-q")
        (sb.root / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (sb.root / ".git" / "info" / "exclude").write_text("\n".join(EXCLUDES) + "\n")
        sb._git("add", "-A")
        sb._git("commit", "-q", "-m", "rewind baseline", "--allow-empty")
        if not spec.venv_python:
            for cmd in spec.install_cmds:
                sb.run(cmd, timeout=600)
        return sb

    # -- git helpers -------------------------------------------------------
    def _git(self, *args: str) -> str:
        env = {**os.environ, **GIT_ENV}
        res = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        if res.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
        return res.stdout

    def _safe(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"path escapes sandbox: {path}")
        return p

    # -- Sandbox API -------------------------------------------------------
    def list_files(self, path: str = ".", max_entries: int = 400) -> list[str]:
        base = self._safe(path)
        if not base.exists():
            raise FileNotFoundError(path)
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
            rel_dir = Path(dirpath).relative_to(self.root)
            for d in dirnames:
                out.append((rel_dir / d).as_posix() + "/")
            for f in sorted(filenames):
                if f.endswith(".pyc"):
                    continue
                out.append((rel_dir / f).as_posix())
            if len(out) >= max_entries:
                out.append(f"... truncated at {max_entries} entries")
                break
        return out

    def read_file(self, path: str) -> str:
        return self._safe(path).read_text(encoding="utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> None:
        p = self._safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # keep the file's existing line endings so the receipt shows only real changes
        crlf = p.exists() and b"\r\n" in p.read_bytes()[:20000]
        text = content.replace("\r\n", "\n")
        if crlf:
            text = text.replace("\n", "\r\n")
        p.write_bytes(text.encode("utf-8"))

    def run(self, cmd: list[str], timeout: int = 120, env: Optional[dict[str, str]] = None) -> RunResult:
        cmd = list(cmd)
        if cmd and cmd[0] == "python":
            cmd[0] = str(self.python)
        src = self.root / "src"
        pypath = os.pathsep.join([str(self.root)] + ([str(src)] if src.is_dir() else []))
        full_env = {**os.environ, "PYTHONPATH": pypath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PY_COLORS": "0", **(env or {})}
        t0 = time.time()
        try:
            res = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=full_env)
            return RunResult(res.returncode, res.stdout, res.stderr, False, round(time.time() - t0, 2))
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return RunResult(-1, out, err + f"\n[timed out after {timeout}s]", True, round(time.time() - t0, 2))

    def diff(self) -> str:
        self._git("add", "-A")
        return self._git("diff", "--cached", "--no-color", "--no-ext-diff")

    def changed_files(self) -> list[str]:
        self._git("add", "-A")
        out = self._git("diff", "--cached", "--name-only")
        return [l.strip() for l in out.splitlines() if l.strip()]

    def close(self) -> None:
        if self.root.exists():
            _rmtree(self.root)
