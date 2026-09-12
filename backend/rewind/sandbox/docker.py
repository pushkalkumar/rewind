"""Docker sandbox: the broken tree inside a container started from REWIND_DOCKER_IMAGE (default python:3.11-slim).

Same contract as local.py, executed through `docker exec`: the tree is streamed in as a tar, `git init` +
baseline commit happen inside /work, `run()` wraps the command in `timeout`, and diffs come from git inside
the container. The container-agnostic helpers at the top (tar builder, find-based walk, run script) are
shared with the Daytona backend so both produce byte-identical list_files/read/write/diff output.
"""
from __future__ import annotations

import io
import os
import posixpath
import shlex
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from . import RunResult, Sandbox, SandboxSpec
from .local import EXCLUDES, GIT_ENV, SKIP_DIRS

DOCKER_IMAGE = os.environ.get("REWIND_DOCKER_IMAGE", "python:3.11-slim")
DOCKER_PYTHON = os.environ.get("REWIND_DOCKER_PYTHON", "python")
PIP_CACHE = os.environ.get("REWIND_PIP_CACHE")
WORKDIR = "/work"

GIT_CONFIG = ["-c", f"user.name={GIT_ENV['GIT_AUTHOR_NAME']}", "-c", f"user.email={GIT_ENV['GIT_AUTHOR_EMAIL']}"]
PY_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PY_COLORS": "0"}
KILL_GRACE = 5  # seconds between `timeout`'s SIGTERM and SIGKILL

# python:*-slim images ship without git; install it once per container when missing.
ENSURE_GIT = "command -v git >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq --no-install-recommends git) >/dev/null"


# -- container-agnostic helpers (also used by daytona.py) ------------------------------------
def tar_tree(tree: Path, compress: bool = False) -> bytes:
    """Tar the tree (skipping SKIP_DIRS like local's copytree) so it extracts at the container's repo root."""
    tree = Path(tree)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if compress else "w") as tar:
        for dirpath, dirnames, filenames in os.walk(tree):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            rel = Path(dirpath).relative_to(tree).as_posix()
            for name in dirnames + sorted(filenames):
                tar.add(Path(dirpath) / name, arcname=posixpath.normpath(posixpath.join(rel, name)), recursive=False)
    return buf.getvalue()


def safe_path(root: str, path: str) -> str:
    """Absolute posix path of `path` inside `root`, refusing escapes (mirrors LocalSandbox._safe)."""
    p = posixpath.normpath(posixpath.join(root, path))
    if p != root and not p.startswith(root.rstrip("/") + "/"):
        raise ValueError(f"path escapes sandbox: {path}")
    return p


def find_command(base: str) -> str:
    """POSIX `find` that prints 'd <abs>' / 'f <abs>' for everything under base, pruning SKIP_DIRS."""
    prune = " -o ".join(f"-name {shlex.quote(d)}" for d in sorted(SKIP_DIRS))
    return (
        f"find {shlex.quote(base)} -mindepth 1 \\( -type d \\( {prune} \\) -prune \\)"
        " -o -type d -exec printf 'd %s\\n' {} + -o -type f -exec printf 'f %s\\n' {} +"
    )


def walk_from_find(root: str, base_rel: str, find_output: str, max_entries: int) -> list[str]:
    """Rebuild LocalSandbox.list_files ordering (os.walk top-down, dirs then files, sorted) from find output."""
    prefix = root.rstrip("/") + "/"
    subdirs: dict[str, list[str]] = {}
    files: dict[str, list[str]] = {}
    for line in find_output.splitlines():
        if len(line) < 3 or line[1] != " " or not line[2:].startswith(prefix):
            continue
        rel = line[2:][len(prefix):]
        parent, name = posixpath.split(rel)
        (subdirs if line[0] == "d" else files).setdefault(parent, []).append(name)

    out: list[str] = []

    def visit(rel_dir: str) -> bool:
        dirs = sorted(d for d in subdirs.get(rel_dir, []) if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for d in dirs:
            out.append(posixpath.join(rel_dir, d) + "/")
        for f in sorted(files.get(rel_dir, [])):
            if f.endswith(".pyc"):
                continue
            out.append(posixpath.join(rel_dir, f))
        if len(out) >= max_entries:
            out.append(f"... truncated at {max_entries} entries")
            return False
        return all(visit(posixpath.join(rel_dir, d)) for d in dirs)

    visit("" if base_rel in (".", "") else base_rel)
    return out


def run_script(timeout: int, env: Optional[dict[str, str]], exec_: bool = True) -> str:
    """Shell snippet that sets PYTHONPATH like local.run (root + root/src if present) then runs "$@" under `timeout`."""
    parts = []
    if not env or "PYTHONPATH" not in env:
        parts.append('PYTHONPATH="$PWD"; [ -d "$PWD/src" ] && PYTHONPATH="$PYTHONPATH:$PWD/src"; export PYTHONPATH')
    parts.append(("exec " if exec_ else "") + f'timeout -k {KILL_GRACE} {int(timeout)} "$@"')
    return "; ".join(parts)


def substitute_python(cmd: list[str], python: str) -> list[str]:
    cmd = list(cmd)
    if cmd and cmd[0] == "python":
        cmd[0] = python
    return cmd


def rel_to_root(root: str, abs_path: str) -> str:
    rel = posixpath.relpath(abs_path, root)
    return "." if rel == "." else rel


# -- backend ----------------------------------------------------------------------------------
class DockerSandbox(Sandbox):
    name = "docker"

    def __init__(self, container: str, python: str = DOCKER_PYTHON, workdir: str = WORKDIR):
        self.container = container
        self.python = python
        self.workdir = workdir

    @classmethod
    def create(cls, spec: SandboxSpec, image: str = DOCKER_IMAGE) -> "DockerSandbox":
        container = "rewind-" + uuid.uuid4().hex[:10]
        volumes = ["-v", f"{PIP_CACHE}:/root/.cache/pip"] if PIP_CACHE else []
        _docker("run", "-d", "--name", container, *volumes, "-w", WORKDIR, image, "sleep", "infinity")
        sb = cls(container)
        try:
            sb._exec("mkdir", "-p", WORKDIR)
            _docker("cp", "-", f"{container}:{WORKDIR}", input=tar_tree(spec.tree))
            sb._exec("sh", "-c", ENSURE_GIT)
            sb._git("init", "-q")
            sb.write_file(".git/info/exclude", "\n".join(EXCLUDES) + "\n")
            sb._git("add", "-A")
            sb._git("commit", "-q", "-m", "rewind baseline", "--allow-empty")
            for cmd in spec.install_cmds:
                sb.run(cmd, timeout=600)
        except Exception:
            sb.close()
            raise
        return sb

    # -- docker/git helpers ---------------------------------------------------
    def _exec(self, *args: str, check: bool = True, timeout: Optional[int] = None, env: Optional[dict[str, str]] = None) -> subprocess.CompletedProcess:
        flags = [f for k, v in (env or {}).items() for f in ("-e", f"{k}={v}")]
        return _docker("exec", "-w", self.workdir, *flags, self.container, *args, check=check, timeout=timeout)

    def _git(self, *args: str) -> str:
        res = self._exec("git", *GIT_CONFIG, *args, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {_text(res.stderr).strip()}")
        return _text(res.stdout)

    def _safe(self, path: str) -> str:
        return safe_path(self.workdir, path)

    # -- Sandbox API ---------------------------------------------------------
    def list_files(self, path: str = ".", max_entries: int = 400) -> list[str]:
        base = self._safe(path)
        if self._exec("test", "-e", base, check=False).returncode != 0:
            raise FileNotFoundError(path)
        res = self._exec("sh", "-c", find_command(base))
        return walk_from_find(self.workdir, rel_to_root(self.workdir, base), _text(res.stdout), max_entries)

    def read_file(self, path: str) -> str:
        res = self._exec("cat", self._safe(path), check=False)
        if res.returncode != 0:
            raise FileNotFoundError(path)
        return _text(res.stdout)

    def write_file(self, path: str, content: str) -> None:
        dest = self._safe(path)
        self._exec("mkdir", "-p", posixpath.dirname(dest))
        fd, tmp = tempfile.mkstemp(prefix="rewind-", suffix=".txt")
        try:
            with open(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            _docker("cp", tmp, f"{self.container}:{dest}")
        finally:
            os.unlink(tmp)

    def run(self, cmd: list[str], timeout: int = 120, env: Optional[dict[str, str]] = None) -> RunResult:
        cmd = substitute_python(cmd, self.python)
        full_env = {**PY_ENV, **(env or {})}
        t0 = time.time()
        try:
            res = self._exec("sh", "-c", run_script(timeout, env), "sh", *cmd, check=False, timeout=timeout + KILL_GRACE + 30, env=full_env)
        except subprocess.TimeoutExpired as e:
            return RunResult(-1, _text(e.stdout), _text(e.stderr) + f"\n[timed out after {timeout}s]", True, round(time.time() - t0, 2))
        out, err = _text(res.stdout), _text(res.stderr)
        if res.returncode == 124:
            return RunResult(-1, out, err + f"\n[timed out after {timeout}s]", True, round(time.time() - t0, 2))
        return RunResult(res.returncode, out, err, False, round(time.time() - t0, 2))

    def diff(self) -> str:
        self._git("add", "-A")
        return self._git("diff", "--cached", "--no-color", "--no-ext-diff")

    def changed_files(self) -> list[str]:
        self._git("add", "-A")
        out = self._git("diff", "--cached", "--name-only")
        return [l.strip() for l in out.splitlines() if l.strip()]

    def close(self) -> None:
        _docker("rm", "-f", self.container, check=False)


def _text(data) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)


def _docker(*args: str, input: Optional[bytes] = None, check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Run a docker CLI command. Bytes in/out so utf-8 and LF survive Windows hosts."""
    res = subprocess.run(["docker", *args], input=input, capture_output=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed: {_text(res.stderr).strip()}")
    return res
