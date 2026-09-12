"""Edge cases for the miner/verifier/pytest parser: node ids with spaces, collection errors, renamed and
deleted test files, non-ASCII git output. The end-to-end tests build a tiny git repo and run the real
verifier against the interpreter running this suite (pytest is importable there by construction)."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from rewind.gitmine import RepoCheckout, find_candidates, git
from rewind.models import Instance, PytestRun
from rewind.models import TestResult as _TestResult
from rewind.pytest_runner import PYTEST_BASE, _split_nodeid, build_cmd, parse_pytest_output, run_pytest
from rewind.verify import (
    RepoEnv,
    _build_broken_state,
    _failed_under,
    _make_runner,
    _stable_baseline,
    _stale_test_paths,
    _test_patch_files,
    verify_candidate,
)

SUMMARY = textwrap.dedent(
    """\
    .F..F                                                                    [100%]
    =================================== FAILURES ===================================
    ______________________________ test_p[c - d] ___________________________________
    E       AssertionError: msg - with dash
    =========================== short test summary info ============================
    PASSED tests/test_ok.py::test_p[a b]
    PASSED tests/test_ok.py::test_p[c - d]
    PASSED tests/test_ok.py::test_p[ünïcode – dash]
    PASSED tests/test_ok.py::TestOuter::TestInner::test_nested[x y]
    XPASS tests/test_ok.py::test_xp - reason
    XFAIL tests/test_ok.py::test_xf[a b] - expected - failure
    ERROR tests/test_bad.py
    ERROR tests/test_bad2.py - ImportError while importing test module 'tests/test_bad2.py' - no module
    ERROR tests/test_ok.py::test_fixture_err[a b] - RuntimeError: setup - boom
    FAILED tests/test_ok.py::test_p[c - d] - AssertionError: msg - with dash
    FAILED tests/test_ok.py::test_err - RuntimeError: boom - here
    FAILED tests/test_ok.py::TestOuter::TestInner::test_nested_fail[k - v] - assert 1 == 2
    SKIPPED [1] tests\\test_skip.py:2: why - not
    SKIPPED [2] tests/test_skip.py:5: inline
    3 failed, 4 passed, 2 skipped, 3 errors in 0.10s
    """
)


def _by_id(stdout: str) -> dict[str, tuple[str, str]]:
    return {t.nodeid: (t.status, t.message) for t in parse_pytest_output(stdout)}


# -- parser ------------------------------------------------------------------

def test_pytest_base_continues_on_collection_errors():
    assert "--continue-on-collection-errors" in PYTEST_BASE
    assert "-rA" in PYTEST_BASE
    cmd = build_cmd(["tests/test_a.py"])
    assert cmd[:3] == ["python", "-m", "pytest"] and cmd[-1] == "tests/test_a.py"


@pytest.mark.parametrize(
    "rest, nodeid, msg",
    [
        ("tests/t.py::test_p[a b]", "tests/t.py::test_p[a b]", ""),
        ("tests/t.py::test_p[c - d]", "tests/t.py::test_p[c - d]", ""),
        ("tests/t.py::test_p[c - d] - AssertionError: msg - x", "tests/t.py::test_p[c - d]", "AssertionError: msg - x"),
        ("tests/t.py::test_x - RuntimeError: boom - here", "tests/t.py::test_x", "RuntimeError: boom - here"),
        ("tests/t.py", "tests/t.py", ""),
        ("tests/t.py - ImportError: a - b", "tests/t.py", "ImportError: a - b"),
        ("tests/t.py::test_n[[1, 2] - [3]] - assert", "tests/t.py::test_n[[1, 2] - [3]]", "assert"),
    ],
)
def test_split_nodeid(rest, nodeid, msg):
    assert _split_nodeid(rest) == (nodeid, msg)


def test_parse_ids_with_spaces_and_dashes():
    got = _by_id(SUMMARY)
    assert got["tests/test_ok.py::test_p[a b]"] == ("passed", "")
    assert got["tests/test_ok.py::test_p[c - d]"] == ("failed", "AssertionError: msg - with dash")
    assert got["tests/test_ok.py::test_err"] == ("failed", "RuntimeError: boom - here")
    assert got["tests/test_ok.py::TestOuter::TestInner::test_nested_fail[k - v]"] == ("failed", "assert 1 == 2")
    assert "tests/test_ok.py::test_p[c" not in got


def test_parse_unicode_and_nested_classes():
    got = _by_id(SUMMARY)
    assert got["tests/test_ok.py::test_p[ünïcode – dash]"] == ("passed", "")
    assert got["tests/test_ok.py::TestOuter::TestInner::test_nested[x y]"] == ("passed", "")


def test_parse_collection_errors_keyed_by_file():
    got = _by_id(SUMMARY)
    assert got["tests/test_bad.py"] == ("error", "")
    assert got["tests/test_bad2.py"] == ("error", "ImportError while importing test module 'tests/test_bad2.py' - no module")
    assert got["tests/test_ok.py::test_fixture_err[a b]"] == ("error", "RuntimeError: setup - boom")


def test_parse_xfail_xpass_and_skipped():
    got = _by_id(SUMMARY)
    assert got["tests/test_ok.py::test_xp"] == ("passed", "reason")
    assert got["tests/test_ok.py::test_xf[a b]"] == ("failed", "expected - failure")
    assert got["tests/test_skip.py"][0] == "skipped"
    assert "tests\\test_skip.py" not in got  # backslash path normalised onto the same key
    assert len(got) == 12


def test_parse_keeps_worst_status_for_duplicate_ids():
    out = "== short test summary info ==\nPASSED tests/t.py::test_a[x y]\nERROR tests/t.py::test_a[x y] - teardown - boom\n1 passed, 1 error in 0.1s\n"
    got = _by_id(out)
    assert got == {"tests/t.py::test_a[x y]": ("error", "teardown - boom")}


# -- real pytest through the runner ----------------------------------------------

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _local_run(cwd: Path):
    def run(cmd, timeout=120, extra_env=None):
        from rewind.sandbox import RunResult

        cmd = [sys.executable if c == "python" else c for c in cmd]
        res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return RunResult(res.returncode, res.stdout, res.stderr, False, 0.0)

    return run


def test_live_pytest_collection_error_does_not_hide_other_results(tmp_path: Path):
    _write(tmp_path / "pytest.ini", "[pytest]\n")
    _write(
        tmp_path / "tests" / "test_ok.py",
        '''
        import pytest

        @pytest.mark.parametrize("x", ["a b", "c - d"])
        def test_p(x):
            assert x != "c - d", "msg - with dash"

        class TestOuter:
            class TestInner:
                def test_nested(self):
                    pass
        ''',
    )
    _write(tmp_path / "tests" / "test_bad.py", "import module_that_does_not_exist_anywhere\n")
    res = run_pytest(_local_run(tmp_path), ["tests"])
    got = {t.nodeid: t for t in res.tests}
    assert got["tests/test_bad.py"].status == "error"
    assert got["tests/test_ok.py::test_p[a b]"].status == "passed"
    assert got["tests/test_ok.py::TestOuter::TestInner::test_nested"].status == "passed"
    assert got["tests/test_ok.py::test_p[c - d]"].status == "failed"
    assert got["tests/test_ok.py::test_p[c - d]"].message.startswith("AssertionError: msg - with dash")
    assert res.passed == {"tests/test_ok.py::test_p[a b]", "tests/test_ok.py::TestOuter::TestInner::test_nested"}


# -- scratch git repos -----------------------------------------------------------

def _g(path: Path, *args: str) -> str:
    return git(path, "-c", "user.name=rewind", "-c", "user.email=rewind@example.com", *args)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _g(path, "init", "-q")
    _g(path, "config", "core.autocrlf", "false")
    _g(path, "config", "advice.detachedHead", "false")


def _commit(path: Path, subject: str) -> str:
    _g(path, "add", "-A")
    _g(path, "commit", "-q", "--allow-empty", "-m", subject)
    return _g(path, "rev-parse", "HEAD").strip()


def _checkout(path: Path) -> RepoCheckout:
    branch = _g(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return RepoCheckout(url="", name="scratch", path=path, default_branch=branch, head_sha=_g(path, "rev-parse", "HEAD").strip())


def _base_repo(path: Path) -> None:
    """A package with a buggy `add`, a passing test module and a pyproject so pytest roots at the repo."""
    _init_repo(path)
    _write(path / "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    _write(path / "pkg" / "__init__.py", "def add(a, b):\n    return a - b\n\n\ndef ident(x):\n    return x\n")
    _write(
        path / "tests" / "test_base.py",
        """
        import pytest
        from pkg import ident

        @pytest.mark.parametrize("v", ["a b", "c - d"])
        def test_ident(v):
            assert ident(v) == v

        def test_plain():
            assert ident(1) == 1
        """,
    )


ENV = RepoEnv(venv_python=Path(sys.executable), install_log="")


def test_verify_import_error_in_broken_state_keeps_regression_baseline(tmp_path: Path):
    repo = tmp_path / "repo"
    _base_repo(repo)
    _commit(repo, "initial")
    _write(repo / "pkg" / "newmod.py", "def double(x):\n    return 2 * x\n")
    _write(repo / "tests" / "test_newmod.py", "from pkg.newmod import double\n\n\ndef test_double():\n    assert double(2) == 4\n")
    sha = _commit(repo, "Fix missing double helper (closes #7)")
    rc = _checkout(repo)
    cands = find_candidates(rc, 50)
    assert [c.sha for c in cands] == [sha]

    inst = verify_candidate(rc, ENV, cands[0], tmp_path / "trees", timeout=120)
    assert isinstance(inst, Instance), getattr(inst, "reason", inst)
    assert inst.fail_to_pass == ["tests/test_newmod.py::test_double"]
    # tests/test_newmod.py cannot even import in the broken tree; without --continue-on-collection-errors the
    # whole-suite baseline aborted and pass_to_pass came back empty.
    assert set(inst.pass_to_pass) == {"tests/test_base.py::test_ident[a b]", "tests/test_base.py::test_ident[c - d]", "tests/test_base.py::test_plain"}
    assert inst.regression_targets == []
    assert inst.test_files == ["tests/test_newmod.py"]
    tree = tmp_path / "trees" / sha[:10]
    assert (tree / "tests" / "test_newmod.py").is_file() and not (tree / "pkg" / "newmod.py").exists()
    assert not (tree / ".git").exists()
    assert "+def double" in inst.gold_patch and "+def test_double" in inst.test_patch
    assert _g(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == rc.default_branch


def _renamed_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    _base_repo(repo)
    old = textwrap.dedent("""
        from pkg import add, ident


        def test_add_zero():
            assert add(1, 0) == 1


        def test_ident_str():
            assert ident("s") == "s"


        def test_ident_none():
            assert ident(None) is None


        def test_ident_list():
            assert ident([1]) == [1]
        """)
    _write(repo / "tests" / "test_old.py", old)
    _write(repo / "tests" / "test_gone.py", "def test_gone():\n    assert True\n")
    _write(repo / "tests" / "data" / "old.txt", "fixture\n")
    _commit(repo, "initial")
    (repo / "tests" / "test_old.py").unlink()
    (repo / "tests" / "test_gone.py").unlink()
    (repo / "tests" / "data" / "old.txt").rename(repo / "tests" / "data" / "new.txt")
    _write(repo / "tests" / "test_new.py", old + "\n\ndef test_add_fixed():\n    assert add(2, 3) == 5\n")
    _write(repo / "pkg" / "__init__.py", "def add(a, b):\n    return a + b\n\n\ndef ident(x):\n    return x\n")
    sha = _commit(repo, "Fix add() and rename the test module")
    return repo, sha


def test_stale_test_paths_detects_renames_and_deletes(tmp_path: Path):
    repo, sha = _renamed_repo(tmp_path)
    rc = _checkout(repo)
    status = _g(repo, "diff", "--name-status", "-M", f"{sha}~1", sha)
    assert any(l.startswith("R") and l.endswith("tests/test_new.py") for l in status.splitlines()), status
    (cand,) = find_candidates(rc, 50)
    assert sorted(cand.test_files) == ["tests/test_gone.py", "tests/test_new.py"]
    assert sorted(_stale_test_paths(rc, cand)) == ["tests/data/old.txt", "tests/test_gone.py", "tests/test_old.py"]

    _build_broken_state(rc, cand, _test_patch_files(rc, cand))
    tests = sorted(p.relative_to(repo).as_posix() for p in (repo / "tests").rglob("*") if p.is_file())
    assert tests == ["tests/data/new.txt", "tests/test_base.py", "tests/test_new.py"]
    assert "return a - b" in (repo / "pkg" / "__init__.py").read_text(encoding="utf-8")  # source stays at the parent
    assert not _g(repo, "ls-files", "--", "tests/test_old.py", "tests/test_gone.py", "tests/data/old.txt").strip()
    _g(repo, "checkout", "-q", "--force", rc.default_branch)


def test_verify_renamed_and_deleted_test_files_end_to_end(tmp_path: Path):
    repo, sha = _renamed_repo(tmp_path)
    rc = _checkout(repo)
    (cand,) = find_candidates(rc, 50)
    inst = verify_candidate(rc, ENV, cand, tmp_path / "trees", timeout=120)
    assert isinstance(inst, Instance), getattr(inst, "reason", inst)
    assert inst.fail_to_pass == ["tests/test_new.py::test_add_fixed"]
    assert inst.test_files == ["tests/test_new.py"]  # tests/test_gone.py no longer exists at the fix commit
    assert inst.regression_targets == []
    assert "tests/test_new.py::test_add_zero" in inst.pass_to_pass and "tests/test_base.py::test_plain" in inst.pass_to_pass
    assert not any(n.startswith(("tests/test_old.py", "tests/test_gone.py")) for n in inst.pass_to_pass)
    tree = tmp_path / "trees" / sha[:10]
    tests = sorted(p.relative_to(tree).as_posix() for p in (tree / "tests").rglob("*") if p.is_file())
    assert tests == ["tests/data/new.txt", "tests/test_base.py", "tests/test_new.py"]
    assert "--- a/tests/test_old.py" in inst.test_patch and "--- a/tests/test_gone.py" in inst.test_patch
    assert "+++ b/tests/test_new.py" in inst.test_patch
    assert "pkg/__init__.py" not in inst.test_patch and "+    return a + b" in inst.gold_patch


CLOCK_TEST = textwrap.dedent('''
    import time
    import pytest
    from pkg import ident

    @pytest.mark.parametrize("v", [f"now {time.time_ns()}", "stable x"])
    def test_clock(v):
        assert ident(v) == v
    ''')


def test_verify_drops_unreproducible_parametrize_ids(tmp_path: Path):
    repo = tmp_path / "repo"
    _base_repo(repo)
    _write(repo / "tests" / "test_clock.py", CLOCK_TEST)  # not a target file: only the full-suite runs see it
    _commit(repo, "initial")
    _write(repo / "pkg" / "__init__.py", "def add(a, b):\n    return a + b\n\n\ndef ident(x):\n    return x\n")
    _write(repo / "tests" / "test_add.py", CLOCK_TEST + "\n\ndef test_add():\n    from pkg import add\n    assert add(2, 3) == 5\n")
    _commit(repo, "Fix add() sign bug")
    rc = _checkout(repo)
    (cand,) = find_candidates(rc, 50)
    inst = verify_candidate(rc, ENV, cand, tmp_path / "trees", timeout=120)
    assert isinstance(inst, Instance), getattr(inst, "reason", inst)
    assert inst.fail_to_pass == ["tests/test_add.py::test_add"]
    assert not any("now " in n for n in inst.pass_to_pass)
    assert {"tests/test_add.py::test_clock[stable x]", "tests/test_clock.py::test_clock[stable x]", "tests/test_base.py::test_plain"} <= set(inst.pass_to_pass)
    assert inst.regression_targets == []


def test_failed_under_matches_exact_id_or_errored_scope():
    not_passed = {"tests/test_a.py", "tests/test_b.py::TestX", "tests/test_c.py::test_x[a b]"}
    assert _failed_under("tests/test_a.py::test_new[x - y]", not_passed)
    assert _failed_under("tests/test_b.py::TestX::test_m", not_passed)
    assert _failed_under("tests/test_c.py::test_x[a b]", not_passed)
    assert not _failed_under("tests/test_c.py::test_x[a c]", not_passed)
    assert not _failed_under("tests/test_a_more.py::test_new", not_passed)
    assert not _failed_under("tests/test_b.py::TestXY::test_m", not_passed)


def _run(passed: list[str], timed_out: bool = False) -> PytestRun:
    return PytestRun(returncode=0, tests=[_TestResult(nodeid=n, status="passed") for n in passed], timed_out=timed_out)


def test_stable_baseline_intersects_both_states_or_falls_back():
    broken_full = _run(["tests/test_t.py::test_a", "tests/test_t.py::test_c[now 1]", "tests/test_o.py::test_b", "tests/test_o.py::test_c[now 2]", "tests/test_o.py::test_gold_breaks"])
    fixed_full = _run(["tests/test_t.py::test_a", "tests/test_t.py::test_c[now 3]", "tests/test_o.py::test_b", "tests/test_o.py::test_c[now 4]"])
    broken_targets = _run(["tests/test_t.py::test_a", "tests/test_t.py::test_c[now 0]"])
    assert _stable_baseline(broken_full, fixed_full, broken_targets, ["tests/test_t.py"]) == {"tests/test_t.py::test_a", "tests/test_o.py::test_b"}
    # fix-commit full run unusable: target-file ids must still be reproducible across the two broken-state runs
    fallback = _stable_baseline(broken_full, _run([], timed_out=True), broken_targets, ["tests/test_t.py"])
    assert fallback == {"tests/test_t.py::test_a", "tests/test_o.py::test_b", "tests/test_o.py::test_c[now 2]", "tests/test_o.py::test_gold_breaks"}


# -- encoding ------------------------------------------------------------------

def test_git_output_is_utf8_and_paths_unquoted(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo / "README.md", "scratch\n")
    _commit(repo, "initial")
    _write(repo / "src" / "naïve.py", "x = 'café'\n")
    _write(repo / "tests" / "test_ünï.py", "def test_x():\n    assert True\n")
    subject = "Fix café ünïcode – en dash € (closes #1)"
    _commit(repo, subject)
    assert _g(repo, "log", "-1", "--format=%s").strip() == subject
    names = _g(repo, "show", "--format=", "--name-only", "HEAD").split()
    assert "src/naïve.py" in names and "tests/test_ünï.py" in names
    assert not any('"' in n or "\\" in n for n in names)
    rc = _checkout(repo)
    (cand,) = find_candidates(rc, 10)
    assert cand.subject == subject and cand.test_files == ["tests/test_ünï.py"] and cand.source_files == ["src/naïve.py"]
    diff = _g(repo, "show", "--format=", "HEAD", "--", "src/naïve.py")
    assert "+x = 'café'" in diff


def test_runner_decodes_utf8_output(tmp_path: Path):
    _init_repo(tmp_path)
    _commit(tmp_path, "initial")
    rc = _checkout(tmp_path)
    res = _make_runner(rc, ENV)(["python", "-c", "print('caf\\u00e9 \\u2013 \\u20ac')"], timeout=60)
    assert res.returncode == 0 and res.stdout.strip() == "café – €"
