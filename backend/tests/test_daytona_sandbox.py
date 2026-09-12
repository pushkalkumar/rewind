"""DaytonaSandbox: no API key here, so the Daytona client is faked with the same attribute paths the backend
calls (client.create, sandbox.get_work_dir, sandbox.fs.upload_file, sandbox.process.exec, sandbox.delete).
The fake records every call AND executes commands for real through Git Bash on the host, so the shell logic
(tar extract, git baseline, find-based listing, timeout capture) is exercised end to end and compared with
LocalSandbox on the same tree."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rewind.sandbox import SandboxSpec
from rewind.sandbox import daytona as dt
from rewind.sandbox.local import EXCLUDES, LocalSandbox

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(not BASH, reason="needs Git Bash to execute the sandbox's shell commands")


def _posix(p: Path) -> str:
    return subprocess.run(["cygpath", "-u", str(p)], capture_output=True, text=True, check=True).stdout.strip()


def _win(p: str) -> str:
    return subprocess.run(["cygpath", "-w", p], capture_output=True, text=True, check=True).stdout.strip()


class FakeResponse:
    def __init__(self, exit_code: int, result: str):
        self.exit_code = exit_code
        self.result = result
        self.artifacts = type("A", (), {"stdout": result})()


class FakeFS:
    def __init__(self, sb: "FakeSandbox"):
        self.sb = sb

    def upload_file(self, src: bytes, dst: str, timeout: int = 1800) -> None:
        self.sb.calls.append(("fs.upload_file", dst, len(src)))
        Path(_win(dst)).write_bytes(src)


class FakeProcess:
    def __init__(self, sb: "FakeSandbox"):
        self.sb = sb

    def exec(self, command: str, cwd=None, env=None, timeout=None) -> FakeResponse:
        self.sb.calls.append(("process.exec", command, cwd, env, timeout))
        full_env = {**os.environ, **(env or {})}
        res = subprocess.run([BASH, "-c", command], cwd=_win(cwd) if cwd else None, env=full_env, capture_output=True, timeout=timeout)
        merged = (res.stdout + res.stderr).decode("utf-8", errors="replace").replace("\r\n", "\n")
        return FakeResponse(res.returncode, merged)


class FakeSandbox:
    def __init__(self, home: Path, sandbox_id: str = "sbx-1"):
        self.id = sandbox_id
        self.home = home
        self.calls: list[tuple] = []
        self.fs = FakeFS(self)
        self.process = FakeProcess(self)
        self.deleted = False

    def get_work_dir(self) -> str:
        self.calls.append(("get_work_dir",))
        return _posix(self.home)

    def delete(self, timeout=60, wait=False) -> None:
        self.calls.append(("delete",))
        self.deleted = True


class FakeClient:
    def __init__(self, home: Path):
        self.home = home
        self.created: list[tuple] = []
        self.sandbox = FakeSandbox(home)

    def create(self, params=None, *, timeout=60, on_snapshot_create_logs=None):
        self.created.append((params, timeout))
        return self.sandbox


def _w(p: Path, text: str) -> None:
    """utf-8 + LF regardless of host platform (what a real repo checkout looks like)."""
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = tmp_path / "tree"
    (t / "src" / "pkg").mkdir(parents=True)
    (t / "tests").mkdir()
    (t / "__pycache__").mkdir()
    (t / "node_modules" / "x").mkdir(parents=True)
    (t / "pkg.egg-info").mkdir()
    _w(t / "src" / "pkg" / "__init__.py", "x = 1\n")
    _w(t / "src" / "pkg" / "core.py", "def f():\n    return 1\n")
    (t / "src" / "pkg" / "core.pyc").write_bytes(b"\x00")
    _w(t / "tests" / "test_core.py", "from pkg.core import f\n\ndef test_f():\n    assert f() == 1\n")
    _w(t / "pyproject.toml", "[project]\nname='pkg'\n")
    _w(t / "README.md", "café ☃\n")
    (t / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    _w(t / "pkg.egg-info" / "PKG-INFO", "x")
    return t


@pytest.fixture
def client(tmp_path: Path) -> FakeClient:
    home = tmp_path / "home"
    home.mkdir()
    return FakeClient(home)


@pytest.fixture
def local(tree: Path):
    """A LocalSandbox over a copy of the same tree, for format comparisons."""
    root = tree.parent / "local"
    shutil.copytree(tree, root)
    sb = LocalSandbox(root, Path(sys.executable))
    sb._git("init", "-q")
    (root / ".git" / "info" / "exclude").write_text("\n".join(EXCLUDES) + "\n")
    sb._git("add", "-A")
    sb._git("commit", "-q", "-m", "rewind baseline", "--allow-empty")
    yield sb
    sb.close()


@pytest.fixture
def sandbox(client: FakeClient, tree: Path, monkeypatch):
    monkeypatch.setattr(dt, "DAYTONA_WORKDIR", None)
    sb = dt.DaytonaSandbox.create(SandboxSpec(tree, install_cmds=[["python", "-c", "print('installed')"]]), client=client)
    yield sb
    sb.close()


def test_make_params_and_client(monkeypatch):
    from daytona import CreateSandboxFromImageParams, CreateSandboxFromSnapshotParams

    monkeypatch.setattr(dt, "DAYTONA_SNAPSHOT", None)
    monkeypatch.setattr(dt, "DAYTONA_IMAGE", None)
    p = dt.make_params("rewind-abc")
    assert isinstance(p, CreateSandboxFromSnapshotParams) and p.language == "python" and p.labels == {"rewind": "rewind-abc"}
    monkeypatch.setattr(dt, "DAYTONA_IMAGE", "python:3.11-slim")
    p = dt.make_params("rewind-abc")
    assert isinstance(p, CreateSandboxFromImageParams) and p.image == "python:3.11-slim" and p.labels == {"rewind": "rewind-abc"}
    monkeypatch.setattr(dt, "DAYTONA_SNAPSHOT", "my-snap")
    p = dt.make_params("rewind-abc")
    assert isinstance(p, CreateSandboxFromSnapshotParams) and p.snapshot == "my-snap"
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        dt.make_client()
    monkeypatch.setenv("DAYTONA_API_KEY", "k")
    monkeypatch.setenv("DAYTONA_API_URL", "https://example.test/api")
    monkeypatch.setenv("DAYTONA_TARGET", "us")
    c = dt.make_client()
    assert type(c).__name__ == "Daytona"


@needs_bash
def test_create_call_sequence(sandbox: dt.DaytonaSandbox, client: FakeClient):
    from daytona import CreateSandboxFromSnapshotParams

    params, timeout = client.created[0]
    assert isinstance(params, CreateSandboxFromSnapshotParams) and timeout == dt.CREATE_TIMEOUT
    assert list(params.labels) == ["rewind"] and params.labels["rewind"].startswith("rewind-") and len(params.labels["rewind"]) == 17
    calls = client.sandbox.calls
    assert calls[0] == ("get_work_dir",)
    work = _posix(client.home) + "/rewind"
    assert sandbox.workdir == work
    assert calls[1][:2] == ("fs.upload_file", work + ".tgz") and calls[1][2] > 0
    unpack = calls[2]
    assert unpack[0] == "process.exec" and unpack[1].startswith(f"mkdir -p {work} && tar -xzf {work}.tgz -C {work} && rm -f") and unpack[2] == _posix(client.home)
    assert calls[3][1] == dt.ENSURE_GIT and calls[3][2] == work
    git = "git -c user.name=rewind -c user.email=rewind@local "
    assert calls[4][1] == git + "init -q"
    assert calls[5][1] == f"mkdir -p {work}/.git/info"
    assert calls[6][:2] == ("fs.upload_file", f"{work}/.git/info/exclude")
    assert calls[7][1] == git + "add -A"
    assert calls[8][1] == git + "commit -q -m 'rewind baseline' --allow-empty"
    install = calls[9]
    assert install[1].startswith("bash -c ") and install[1].endswith(" bash python -c 'print('\"'\"'installed'\"'\"')'")
    assert install[3] == {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PY_COLORS": "0"} and install[4] == 600 + 5 + 30
    assert len(calls) == 10
    # the tree really landed and the archive was removed
    assert (client.home / "rewind" / "src" / "pkg" / "core.py").read_text() == "def f():\n    return 1\n"
    assert not (client.home / "rewind.tgz").exists()
    assert not (client.home / "rewind" / "node_modules").exists()
    assert (client.home / "rewind" / ".git" / "info" / "exclude").read_bytes() == ("\n".join(EXCLUDES) + "\n").encode()


@needs_bash
@pytest.mark.parametrize("base", [".", "src", "src/pkg", "tests"])
def test_list_files_matches_local(sandbox: dt.DaytonaSandbox, local: LocalSandbox, base: str):
    assert sandbox.list_files(base) == local.list_files(base)
    for cap in (1, 2, 4, 100):
        assert sandbox.list_files(base, max_entries=cap) == local.list_files(base, max_entries=cap)
    with pytest.raises(FileNotFoundError):
        sandbox.list_files("nope")
    with pytest.raises(ValueError):
        sandbox.list_files("../..")


@needs_bash
def test_read_write_roundtrip_matches_local(sandbox: dt.DaytonaSandbox, local: LocalSandbox):
    assert sandbox.read_file("README.md") == local.read_file("README.md") == "café ☃\n"
    content = "# café ☃\nif True:\n    pass\n"
    sandbox.write_file("src/pkg/new/mod.py", content)
    local.write_file("src/pkg/new/mod.py", content)
    assert sandbox.read_file("src/pkg/new/mod.py") == local.read_file("src/pkg/new/mod.py") == content
    assert (sandbox.sandbox.home / "rewind" / "src" / "pkg" / "new" / "mod.py").read_bytes() == content.encode("utf-8")  # LF, utf-8
    assert sandbox.sandbox.calls[-2][:2] == ("fs.upload_file", sandbox.workdir + "/src/pkg/new/mod.py")
    assert sandbox.list_files("src/pkg") == local.list_files("src/pkg")
    with pytest.raises(FileNotFoundError):
        sandbox.read_file("missing.py")
    with pytest.raises(ValueError):
        sandbox.write_file("../escape.py", "x")


@needs_bash
def test_diff_and_changed_files_match_local(sandbox: dt.DaytonaSandbox, local: LocalSandbox):
    assert sandbox.diff() == local.diff() == ""
    assert sandbox.changed_files() == local.changed_files() == []
    for sb in (sandbox, local):
        sb.write_file("src/pkg/core.py", "def f():\n    return 2\n")
        sb.write_file("src/pkg/extra.py", "y = 1\n")
        sb.write_file("__pycache__/x.pyc", "ignored")
    assert sandbox.changed_files() == local.changed_files() == ["src/pkg/core.py", "src/pkg/extra.py"]
    d = sandbox.diff()
    assert d == local.diff()
    assert "-    return 1\n+    return 2\n" in d and "new file mode" in d and "__pycache__" not in d


@needs_bash
def test_run_separates_streams_and_substitutes_python(sandbox: dt.DaytonaSandbox, monkeypatch):
    monkeypatch.setattr(sandbox, "python", sys.executable.replace("\\", "/"))
    res = sandbox.run(["python", "-c", "import sys; print('out'); sys.stderr.write('err\\n'); sys.exit(3)"], timeout=30, env={"FOO": "bar"})
    assert (res.returncode, res.stdout, res.stderr, res.timed_out) == (3, "out\n", "err\n", False)
    call = sandbox.sandbox.calls[-1]
    assert call[0] == "process.exec" and call[2] == sandbox.workdir and call[4] == 30 + 5 + 30
    assert call[3] == {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PY_COLORS": "0", "FOO": "bar"}
    assert call[1].startswith("bash -c ") and "timeout -k 5 30 \"$@\"" in call[1] and '"$0"' not in call[1]
    assert "python" not in call[1].split(" bash ")[-1] or sys.executable.replace("\\", "/") in call[1]
    # PYTHONPATH = root + root/src, like local.run (MSYS rewrites the posix list into Windows form for a native exe)
    res = sandbox.run(["python", "-c", "import os; print(os.environ['PYTHONPATH'])"])
    parts = [p.replace("\\", "/").split("/home/")[-1] for p in re.split(r"[;:](?![/\\])", res.stdout.strip())]
    assert parts == ["rewind", "rewind/src"]
    res = sandbox.run(["python", "-c", "import os; print(os.environ['PYTHONPATH'])"], env={"PYTHONPATH": "custom_value"})
    assert res.stdout.strip() == "custom_value"


@needs_bash
def test_run_timeout(sandbox: dt.DaytonaSandbox):
    res = sandbox.run(["sleep", "5"], timeout=1)
    assert res.timed_out and res.returncode == -1 and "[timed out after 1s]" in res.stderr and res.seconds < 5


@needs_bash
def test_close_deletes_sandbox(client: FakeClient, tree: Path):
    sb = dt.DaytonaSandbox.create(SandboxSpec(tree, install_cmds=[["true"]]), client=client)
    sb.close()
    assert client.sandbox.deleted and client.sandbox.calls[-1] == ("delete",)


def test_parse_capture():
    assert dt.parse_capture("out\n\n@@REWIND_STDERR@@\nerr\n\n@@REWIND_RC@@ 3\n") == ("out\n", "err\n", 3)
    assert dt.parse_capture("\n@@REWIND_STDERR@@\n\n@@REWIND_RC@@ 0\n") == ("", "", 0)
    assert dt.parse_capture("garbage") == ("garbage", "", -1)
    assert dt.parse_capture("o\n@@REWIND_STDERR@@\nno rc") == ("o", "no rc", -1)
