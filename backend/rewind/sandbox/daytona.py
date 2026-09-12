"""Daytona sandbox: the broken tree inside a remote Daytona sandbox driven by the official `daytona` Python SDK.

SDK surface used (verified against daytona==0.211.2 and https://www.daytona.io/docs/python-sdk/):
  - Daytona(DaytonaConfig(api_key=..., api_url=..., target=...))      python-sdk/sync/daytona  "DaytonaConfig"
  - daytona.create(CreateSandboxFromImageParams(image=..., labels=...) | CreateSandboxFromSnapshotParams(...), timeout=...)
                                                                        python-sdk/sync/daytona  "Daytona.create"
  - sandbox.get_work_dir() -> str                                       python-sdk/sync/sandbox  "get_work_dir"
  - sandbox.fs.upload_file(src: bytes, dst: str)                        python-sdk/sync/file-system "upload_file"
  - sandbox.process.exec(command, cwd=..., env=..., timeout=...) -> ExecuteResponse(exit_code, result)
                                                                        python-sdk/sync/process  "exec"
    `result` is the merged stdout of the shell command; there is no separate stderr channel, so run()
    redirects the command's streams to files and prints them back with markers.
  - sandbox.delete()                                                    python-sdk/sync/sandbox  "delete"

The tree goes up as one tar.gz and is extracted inside (one upload instead of one per file), then
`git init` + baseline commit happen exactly like local.py.
"""
from __future__ import annotations

import os
import posixpath
import shlex
import time
import uuid
from typing import Any, Optional

from . import RunResult, Sandbox, SandboxSpec
from .docker import ENSURE_GIT, GIT_CONFIG, KILL_GRACE, PY_ENV, find_command, rel_to_root, run_script, safe_path, substitute_python, tar_tree, walk_from_find
from .local import EXCLUDES

DAYTONA_IMAGE = os.environ.get("DAYTONA_IMAGE")  # e.g. python:3.11-slim -> CreateSandboxFromImageParams
DAYTONA_SNAPSHOT = os.environ.get("DAYTONA_SNAPSHOT")  # a prebuilt snapshot name -> CreateSandboxFromSnapshotParams
DAYTONA_PYTHON = os.environ.get("DAYTONA_PYTHON", "python")
DAYTONA_WORKDIR = os.environ.get("DAYTONA_WORKDIR")  # absolute dir for the repo; default <sandbox work dir>/rewind
CREATE_TIMEOUT = int(os.environ.get("DAYTONA_CREATE_TIMEOUT", "300"))

STDERR_MARK = "@@REWIND_STDERR@@"
RC_MARK = "@@REWIND_RC@@"


def make_client():
    """Daytona() from DAYTONA_API_KEY (+ optional DAYTONA_API_URL / DAYTONA_TARGET)."""
    from daytona import Daytona, DaytonaConfig

    key = os.environ.get("DAYTONA_API_KEY")
    if not key:
        raise RuntimeError("DAYTONA_API_KEY is not set")
    cfg: dict[str, Any] = {"api_key": key}
    if os.environ.get("DAYTONA_API_URL"):
        cfg["api_url"] = os.environ["DAYTONA_API_URL"]
    if os.environ.get("DAYTONA_TARGET"):
        cfg["target"] = os.environ["DAYTONA_TARGET"]
    return Daytona(DaytonaConfig(**cfg))


def make_params(label: str):
    """Snapshot params if DAYTONA_SNAPSHOT is set, image params if DAYTONA_IMAGE is set, else Daytona's python snapshot."""
    from daytona import CreateSandboxFromImageParams, CreateSandboxFromSnapshotParams

    labels = {"rewind": label}
    if DAYTONA_SNAPSHOT:
        return CreateSandboxFromSnapshotParams(snapshot=DAYTONA_SNAPSHOT, labels=labels)
    if DAYTONA_IMAGE:
        return CreateSandboxFromImageParams(image=DAYTONA_IMAGE, labels=labels)
    return CreateSandboxFromSnapshotParams(language="python", labels=labels)


def capture_script(timeout: int, env: Optional[dict[str, str]], tag: str) -> str:
    """run_script() variant that keeps stdout/stderr apart by writing them to files and echoing them with markers."""
    out, err = f"/tmp/rw-{tag}.out", f"/tmp/rw-{tag}.err"
    body = run_script(timeout, env, exec_=False)  # the shell must stay alive to print the streams
    return (
        f"{body} >{out} 2>{err}; rc=$?; cat {out}; printf '\\n{STDERR_MARK}\\n'; cat {err}; "
        f"printf '\\n{RC_MARK} %s\\n' \"$rc\"; rm -f {out} {err}"
    )


def parse_capture(result: str) -> tuple[str, str, int]:
    """Split the marked output back into (stdout, stderr, returncode)."""
    stdout, sep, rest = result.partition(f"\n{STDERR_MARK}\n")
    if not sep:
        return result, "", -1
    stderr, sep, rc = rest.rpartition(f"\n{RC_MARK} ")
    if not sep:
        return stdout, rest, -1
    try:
        return stdout, stderr, int(rc.strip())
    except ValueError:
        return stdout, stderr, -1


class DaytonaSandbox(Sandbox):
    name = "daytona"

    def __init__(self, client, sandbox, workdir: str, python: str = DAYTONA_PYTHON):
        self.client = client
        self.sandbox = sandbox
        self.workdir = workdir
        self.python = python

    @classmethod
    def create(cls, spec: SandboxSpec, client=None) -> "DaytonaSandbox":
        client = client or make_client()
        label = "rewind-" + uuid.uuid4().hex[:10]
        sandbox = client.create(make_params(label), timeout=CREATE_TIMEOUT)
        workdir = DAYTONA_WORKDIR or posixpath.join(sandbox.get_work_dir(), "rewind")
        sb = cls(client, sandbox, posixpath.normpath(workdir))
        try:
            archive = sb.workdir + ".tgz"
            sandbox.fs.upload_file(tar_tree(spec.tree, compress=True), archive)
            unpack = f"mkdir -p {shlex.quote(sb.workdir)} && tar -xzf {shlex.quote(archive)} -C {shlex.quote(sb.workdir)} && rm -f {shlex.quote(archive)}"
            sb._sh(unpack, cwd=posixpath.dirname(sb.workdir))  # workdir does not exist yet
            sb._sh(ENSURE_GIT)
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

    # -- exec/git helpers ----------------------------------------------------
    def _exec(self, command: str, timeout: Optional[int] = None, env: Optional[dict[str, str]] = None, cwd: Optional[str] = None):
        return self.sandbox.process.exec(command, cwd=cwd or self.workdir, env=env, timeout=timeout)

    def _sh(self, command: str, timeout: int = 600, cwd: Optional[str] = None) -> str:
        res = self._exec(command, timeout=timeout, cwd=cwd)
        if res.exit_code != 0:
            raise RuntimeError(f"{command[:80]} failed ({res.exit_code}): {(res.result or '').strip()[-500:]}")
        return res.result or ""

    def _git(self, *args: str) -> str:
        res = self._exec(shlex.join(["git", *GIT_CONFIG, *args]), timeout=120)
        if res.exit_code != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {(res.result or '').strip()}")
        return res.result or ""

    def _safe(self, path: str) -> str:
        return safe_path(self.workdir, path)

    # -- Sandbox API ---------------------------------------------------------
    def list_files(self, path: str = ".", max_entries: int = 400) -> list[str]:
        base = self._safe(path)
        if self._exec(f"test -e {shlex.quote(base)}", timeout=30).exit_code != 0:
            raise FileNotFoundError(path)
        out = self._sh(find_command(base), timeout=120)
        return walk_from_find(self.workdir, rel_to_root(self.workdir, base), out, max_entries)

    def read_file(self, path: str) -> str:
        res = self._exec(f"cat {shlex.quote(self._safe(path))}", timeout=60)
        if res.exit_code != 0:
            raise FileNotFoundError(path)
        return res.result or ""

    def write_file(self, path: str, content: str) -> None:
        dest = self._safe(path)
        self._sh(f"mkdir -p {shlex.quote(posixpath.dirname(dest))}", timeout=30)
        self.sandbox.fs.upload_file(content.encode("utf-8"), dest)

    def run(self, cmd: list[str], timeout: int = 120, env: Optional[dict[str, str]] = None) -> RunResult:
        cmd = substitute_python(cmd, self.python)
        script = capture_script(timeout, env, uuid.uuid4().hex[:8])
        command = shlex.join(["bash", "-c", script, "bash", *cmd])
        t0 = time.time()
        try:
            res = self._exec(command, timeout=timeout + KILL_GRACE + 30, env={**PY_ENV, **(env or {})})
        except Exception as e:  # the in-sandbox `timeout` fires first; reaching the SDK timeout means the sandbox hung
            return RunResult(-1, "", f"{e}\n[timed out after {timeout}s]", True, round(time.time() - t0, 2))
        out, err, rc = parse_capture(res.result or "")
        if rc == 124:
            return RunResult(-1, out, err + f"\n[timed out after {timeout}s]", True, round(time.time() - t0, 2))
        return RunResult(rc, out, err, False, round(time.time() - t0, 2))

    def diff(self) -> str:
        self._git("add", "-A")
        return self._git("diff", "--cached", "--no-color", "--no-ext-diff")

    def changed_files(self) -> list[str]:
        self._git("add", "-A")
        out = self._git("diff", "--cached", "--name-only")
        return [l.strip() for l in out.splitlines() if l.strip()]

    def close(self) -> None:
        try:
            self.sandbox.delete()
        except Exception:
            pass
