"""DockerSandbox: docker is not installed here, so subprocess.run is replaced by a recorder that answers with
canned output. Every Sandbox method is checked for the exact `docker ...` argv it issues, and list_files is
checked against LocalSandbox on the same real tree (the canned `find` output comes from running the backend's
own find command under Git Bash when available)."""
from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from rewind.sandbox import SandboxSpec
from rewind.sandbox import docker as dk
from rewind.sandbox.local import EXCLUDES, LocalSandbox

BASH = shutil.which("bash")


class FakeDocker:
    """Records every subprocess.run argv and replies from a small table of canned responses."""

    def __init__(self):
        self.real_run = subprocess.run
        self.calls: list[list[str]] = []
        self.inputs: dict[int, bytes] = {}  # call index -> stdin bytes
        self.copied: dict[int, bytes] = {}  # call index -> bytes of the host file given to `docker cp`
        self.find_output = b""
        self.files: dict[str, bytes] = {}
        self.missing: set[str] = set()
        self.diff = b""
        self.exec_rc = 0

    def __call__(self, argv, input=None, capture_output=True, timeout=None, **kw):
        if argv[0] != "docker":  # the tests themselves shell out (cygpath, bash)
            return self.real_run(argv, input=input, capture_output=capture_output, timeout=timeout, **kw)
        idx = len(self.calls)
        self.calls.append(list(argv))
        if input is not None:
            self.inputs[idx] = input
        sub = argv[1]
        if sub == "run":
            return subprocess.CompletedProcess(argv, 0, b"abc123\n", b"")
        if sub == "cp" and argv[2] != "-":
            self.copied[idx] = Path(argv[2]).read_bytes()
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if sub == "exec":
            cmd = argv[argv.index(self.container) + 1 :]
            if cmd[:2] == ["test", "-e"]:
                return subprocess.CompletedProcess(argv, 1 if cmd[2] in self.missing else 0, b"", b"")
            if cmd[:2] == ["sh", "-c"] and cmd[2].startswith("find "):
                return subprocess.CompletedProcess(argv, 0, self.find_output, b"")
            if cmd[0] == "cat":
                if cmd[1] in self.files:
                    return subprocess.CompletedProcess(argv, 0, self.files[cmd[1]], b"")
                return subprocess.CompletedProcess(argv, 1, b"", b"cat: no such file\n")
            if cmd[0] == "git" and "diff" in cmd:
                return subprocess.CompletedProcess(argv, 0, self.diff, b"")
            if cmd[:2] == ["sh", "-c"] and "timeout" in cmd[2]:
                return subprocess.CompletedProcess(argv, self.exec_rc, b"out\n", b"err\n")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    container = "rewind-" + "x" * 10  # what create() names it with uuid4 patched to all-x

    def execs(self) -> list[list[str]]:
        """The command part of every `docker exec` call."""
        return [c[c.index(self.container) + 1 :] for c in self.calls if c[1] == "exec"]


@pytest.fixture
def fake(monkeypatch):
    f = FakeDocker()
    monkeypatch.setattr(subprocess, "run", f)
    monkeypatch.setattr(dk.uuid, "uuid4", lambda: type("U", (), {"hex": "x" * 32})())
    return f


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = tmp_path / "tree"
    (t / "src" / "pkg").mkdir(parents=True)
    (t / "tests").mkdir()
    (t / "__pycache__").mkdir()
    (t / "node_modules" / "x").mkdir(parents=True)
    (t / "pkg.egg-info").mkdir()
    (t / "src" / "pkg" / "__init__.py").write_text("x = 1\n")
    (t / "src" / "pkg" / "core.py").write_text("def f():\n    return 1\n")
    (t / "src" / "pkg" / "core.pyc").write_bytes(b"\x00")
    (t / "tests" / "test_core.py").write_text("from pkg.core import f\n\ndef test_f():\n    assert f() == 1\n")
    (t / "pyproject.toml").write_text("[project]\nname='pkg'\n")
    (t / "README.md").write_text("hi\n")
    (t / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    (t / "pkg.egg-info" / "PKG-INFO").write_text("x")
    return t


def test_create_issues_expected_docker_sequence(fake: FakeDocker, tree: Path, monkeypatch):
    monkeypatch.setattr(dk, "PIP_CACHE", "C:/pipcache")
    sb = dk.DockerSandbox.create(SandboxSpec(tree))
    assert sb.container == "rewind-" + "x" * 10 and sb.workdir == "/work"
    c = fake.calls
    assert c[0] == ["docker", "run", "-d", "--name", fake.container, "-v", "C:/pipcache:/root/.cache/pip", "-w", "/work", "python:3.11-slim", "sleep", "infinity"]
    assert c[1] == ["docker", "exec", "-w", "/work", fake.container, "mkdir", "-p", "/work"]
    assert c[2] == ["docker", "cp", "-", f"{fake.container}:/work"]
    names = sorted(tarfile.open(fileobj=io.BytesIO(fake.inputs[2])).getnames())
    assert names == ["README.md", "pkg.egg-info", "pkg.egg-info/PKG-INFO", "pyproject.toml", "src", "src/pkg", "src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/core.pyc", "tests", "tests/test_core.py"]
    ex = fake.execs()
    assert ex[1] == ["sh", "-c", dk.ENSURE_GIT]
    git = ["git", "-c", "user.name=rewind", "-c", "user.email=rewind@local"]
    assert ex[2] == git + ["init", "-q"]
    assert ex[3] == ["mkdir", "-p", "/work/.git/info"]
    cp = [i for i, call in enumerate(c) if call[1] == "cp" and call[2] != "-"]
    assert c[cp[0]][3] == f"{fake.container}:/work/.git/info/exclude"
    assert fake.copied[cp[0]] == ("\n".join(EXCLUDES) + "\n").encode()
    assert ex[4] == git + ["add", "-A"]
    assert ex[5] == git + ["commit", "-q", "-m", "rewind baseline", "--allow-empty"]
    install = [call for call in c if call[1] == "exec" and "pip" in call]
    assert len(install) == 1
    flags = install[0][: install[0].index(fake.container)]
    assert flags[:4] == ["docker", "exec", "-w", "/work"]
    assert set(flags[4:]) == {"-e", "PYTHONDONTWRITEBYTECODE=1", "PYTHONIOENCODING=utf-8", "PY_COLORS=0"}
    cmd = install[0][install[0].index(fake.container) + 1 :]
    assert cmd[:2] == ["sh", "-c"] and cmd[3:] == ["sh", "python", "-m", "pip", "install", "-q", "-e", ".", "pytest"]
    assert cmd[2] == 'PYTHONPATH="$PWD"; [ -d "$PWD/src" ] && PYTHONPATH="$PYTHONPATH:$PWD/src"; export PYTHONPATH; exec timeout -k 5 600 "$@"'


def test_create_without_pip_cache_has_no_volume(fake: FakeDocker, tree: Path, monkeypatch):
    monkeypatch.setattr(dk, "PIP_CACHE", None)
    dk.DockerSandbox.create(SandboxSpec(tree, install_cmds=[["python", "-c", "pass"]]))
    assert "-v" not in fake.calls[0]
    assert fake.execs()[-1][3:] == ["sh", "python", "-c", "pass"]


def test_run_substitutes_python_and_passes_env(fake: FakeDocker):
    sb = dk.DockerSandbox(fake.container)
    res = sb.run(["python", "-m", "pytest", "-q"], timeout=42, env={"FOO": "bar"})
    call = fake.calls[-1]
    assert call[:4] == ["docker", "exec", "-w", "/work"]
    assert "FOO=bar" in call and "PY_COLORS=0" in call
    cmd = call[call.index(fake.container) + 1 :]
    assert cmd[3:] == ["sh", "python", "-m", "pytest", "-q"]
    assert 'exec timeout -k 5 42 "$@"' in cmd[2]
    assert (res.returncode, res.stdout, res.stderr, res.timed_out) == (0, "out\n", "err\n", False)


def test_run_honours_pythonpath_override(fake: FakeDocker):
    sb = dk.DockerSandbox(fake.container)
    sb.run(["python", "x.py"], env={"PYTHONPATH": "/elsewhere"})
    cmd = fake.execs()[-1]
    assert cmd[2] == 'exec timeout -k 5 120 "$@"' and "PYTHONPATH=/elsewhere" in fake.calls[-1]


def test_run_timeout_maps_124(fake: FakeDocker):
    fake.exec_rc = 124
    res = dk.DockerSandbox(fake.container).run(["python", "-c", "pass"], timeout=7)
    assert res.timed_out and res.returncode == -1 and "[timed out after 7s]" in res.stderr and res.stdout == "out\n"


def test_read_file_uses_cat_and_utf8(fake: FakeDocker):
    fake.files["/work/src/pkg/core.py"] = "caf\u00e9\nline2\n".encode("utf-8")
    sb = dk.DockerSandbox(fake.container)
    assert sb.read_file("src/pkg/core.py") == "caf\u00e9\nline2\n"
    assert fake.execs()[-1] == ["cat", "/work/src/pkg/core.py"]
    with pytest.raises(FileNotFoundError):
        sb.read_file("nope.py")
    with pytest.raises(ValueError):
        sb.read_file("../etc/passwd")


def test_write_file_preserves_utf8_and_lf(fake: FakeDocker):
    sb = dk.DockerSandbox(fake.container)
    sb.write_file("src/new/mod.py", "# caf\u00e9\nprint('x')\n")
    assert fake.execs()[-1] == ["mkdir", "-p", "/work/src/new"]
    cp = fake.calls[-1]
    assert cp[:2] == ["docker", "cp"] and cp[3] == f"{fake.container}:/work/src/new/mod.py"
    assert fake.copied[len(fake.calls) - 1] == "# caf\u00e9\nprint('x')\n".encode("utf-8")
    assert not Path(cp[2]).exists()  # temp file cleaned up


def test_diff_and_changed_files_use_git_inside(fake: FakeDocker):
    fake.diff = b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    sb = dk.DockerSandbox(fake.container)
    assert sb.diff() == fake.diff.decode()
    git = ["git", "-c", "user.name=rewind", "-c", "user.email=rewind@local"]
    assert fake.execs()[-2:] == [git + ["add", "-A"], git + ["diff", "--cached", "--no-color", "--no-ext-diff"]]
    fake.diff = b"src/a.py\n\nsrc/b.py\n"
    assert sb.changed_files() == ["src/a.py", "src/b.py"]
    assert fake.execs()[-1] == git + ["diff", "--cached", "--name-only"]


def test_close_removes_container(fake: FakeDocker):
    dk.DockerSandbox(fake.container).close()
    assert fake.calls[-1] == ["docker", "rm", "-f", fake.container]


def test_list_files_missing_path(fake: FakeDocker):
    fake.missing.add("/work/nope")
    with pytest.raises(FileNotFoundError):
        dk.DockerSandbox(fake.container).list_files("nope")
    assert fake.execs()[-1] == ["test", "-e", "/work/nope"]


def _find_on_host(tree: Path, base_rel: str) -> tuple[str, str]:
    """Run the backend's own find command under Git Bash; returns (posix root, output)."""
    root = subprocess.run(["cygpath", "-u", str(tree)], capture_output=True, text=True, check=True).stdout.strip()
    base = dk.safe_path(root, base_rel)
    out = subprocess.run([BASH, "-c", dk.find_command(base)], capture_output=True, check=True).stdout
    return root, out.decode("utf-8")


@pytest.mark.skipif(not BASH, reason="needs Git Bash for a real `find`")
@pytest.mark.parametrize("base_rel", [".", "src", "src/pkg"])
def test_list_files_matches_local(fake: FakeDocker, tree: Path, base_rel: str):
    root, out = _find_on_host(tree, base_rel)
    expected = LocalSandbox(tree, Path("python")).list_files(base_rel)
    sb = dk.DockerSandbox(fake.container, workdir=root)
    fake.find_output = out.encode("utf-8")
    assert sb.list_files(base_rel) == expected
    assert len(expected) >= 2
    find_call = fake.execs()[-1]
    assert find_call[:2] == ["sh", "-c"] and find_call[2] == dk.find_command(dk.safe_path(root, base_rel))


@pytest.mark.skipif(not BASH, reason="needs Git Bash for a real `find`")
def test_list_files_truncation_matches_local(fake: FakeDocker, tree: Path):
    root, out = _find_on_host(tree, ".")
    fake.find_output = out.encode("utf-8")
    sb = dk.DockerSandbox(fake.container, workdir=root)
    for cap in (1, 3, 5, 100):
        assert sb.list_files(".", max_entries=cap) == LocalSandbox(tree, Path("python")).list_files(".", max_entries=cap)


def test_walk_from_find_pure():
    out = "d /work/src\nf /work/README.md\nd /work/src/pkg\nf /work/src/pkg/a.py\nf /work/src/pkg/a.pyc\nd /work/x.egg-info\nf /work/x.egg-info/P\n"
    assert dk.walk_from_find("/work", ".", out, 400) == ["src/", "README.md", "src/pkg/", "src/pkg/a.py"]
    assert dk.walk_from_find("/work", "src", out, 400) == ["src/pkg/", "src/pkg/a.py"]
    assert dk.walk_from_find("/work", ".", out, 2) == ["src/", "README.md", "... truncated at 2 entries"]


def test_safe_path():
    assert dk.safe_path("/work", ".") == "/work"
    assert dk.safe_path("/work", "a/../b") == "/work/b"
    with pytest.raises(ValueError):
        dk.safe_path("/work", "../x")
    with pytest.raises(ValueError):
        dk.safe_path("/work", "/etc/passwd")
