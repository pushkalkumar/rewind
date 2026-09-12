"""Agent loop mechanics against a synthetic broken repo with scripted drivers: tools, arg normalization, flags.

The synthetic repo, instance and ScriptedDriver live here; tests/test_agent_live.py reuses them with the real CLI.
"""
from __future__ import annotations

import sys
from pathlib import Path

from rewind.agent import READ_CAP, ToolBox, first_message, normalize_args, run_agent, system_prompt
from rewind.events import EventSink
from rewind.llm import Action, LLMDriver
from rewind.models import Candidate, Instance
from rewind.pytest_runner import PYTEST_BASE
from rewind.sandbox import RunResult, SandboxSpec
from rewind.sandbox.local import LocalSandbox
from rewind.scoring import apply_cheat

PRICING = '''"""Price helpers."""


def apply_discount(price: float, pct: float) -> float:
    if not 0 <= pct <= 100:
        raise ValueError("pct must be within 0..100")
    return round(price * (1 - pct / 100), 2)


def line_total(price: float, qty: int) -> float:
    return round(price * qty, 2)
'''

CART_BROKEN = '''"""Shopping cart."""
from .pricing import apply_discount, line_total


class Cart:
    def __init__(self) -> None:
        self.items: list[tuple[str, float, int]] = []

    def add(self, name: str, price: float, qty: int = 1) -> None:
        self.items.append((name, price, qty))

    def subtotal(self) -> float:
        return round(sum(price for _, price, _ in self.items), 2)

    def total(self, discount_pct: float = 0) -> float:
        return apply_discount(self.subtotal(), discount_pct)
'''

TESTS = '''from inventory.cart import Cart
from inventory.pricing import apply_discount


def test_empty_cart():
    assert Cart().subtotal() == 0


def test_subtotal_counts_quantity():
    cart = Cart()
    cart.add("pen", 1.5, qty=4)
    cart.add("book", 10.0)
    assert cart.subtotal() == 16.0


def test_discount_applies():
    assert apply_discount(200, 25) == 150
    cart = Cart()
    cart.add("lamp", 40.0)
    assert cart.total(10) == 36.0
'''

F2P = "tests/test_cart.py::test_subtotal_counts_quantity"
P2P = ["tests/test_cart.py::test_empty_cart", "tests/test_cart.py::test_discount_applies"]

BROKEN_LINE = "        return round(sum(price for _, price, _ in self.items), 2)"
FIXED_LINE = "        return round(sum(line_total(price, qty) for _, price, qty in self.items), 2)"
CART_FIXED = CART_BROKEN.replace(BROKEN_LINE, FIXED_LINE)

PAD_CHARS = 70_000  # > READ_CAP: a whole-file rewrite through the CLI would be impractical, read_file gets truncated


def padded_cart(pad_chars: int = PAD_CHARS) -> str:
    """CART_BROKEN with a wall of comment lines between the imports and the class, like a big real-world module."""
    lines = []
    i = 0
    while sum(len(l) + 1 for l in lines) < pad_chars:
        i += 1
        lines.append(f"# design note {i:05d}: historical context about the cart module kept for reference; not code.")
    head, _, tail = CART_BROKEN.partition("\n\n\nclass Cart:")
    return head + "\n\n" + "\n".join(lines) + "\n\n\nclass Cart:" + tail


def _put(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")  # LF like a git checkout; the sandbox writes LF too


def make_tree(root: Path, cart: str = CART_BROKEN) -> Path:
    (root / "inventory").mkdir(parents=True)
    (root / "tests").mkdir()
    _put(root / "inventory" / "__init__.py", "")
    _put(root / "inventory" / "pricing.py", PRICING)
    _put(root / "inventory" / "cart.py", cart)
    _put(root / "tests" / "test_cart.py", TESTS)
    _put(root / "README.md", "# inventory\n\nToy cart package used by the Rewind agent smoke test.\n")
    return root


def make_instance() -> Instance:
    cand = Candidate(sha="abc1234def", parent_sha="0000000", subject="Fix cart subtotal ignoring quantity",
                     message="Fix cart subtotal ignoring quantity\n\nCart.subtotal() summed unit prices only.",
                     author_date="2026-01-01T00:00:00Z", test_files=["tests/test_cart.py"], source_files=["inventory/cart.py"])
    return Instance(id="abc1234", candidate=cand, issue_text=cand.message, fail_to_pass=[F2P], pass_to_pass=P2P,
                    test_patch="", gold_patch="", test_files=["tests/test_cart.py"])


def make_sandbox(tmp_path: Path, cart: str = CART_BROKEN) -> LocalSandbox:
    return LocalSandbox.create(SandboxSpec(tree=make_tree(tmp_path / "tree", cart), venv_python=Path(sys.executable)))


def _changed_lines(diff: str) -> list[str]:
    """The +/- body lines of a unified diff (headers excluded)."""
    return [l for l in diff.split("\n") if l[:1] in ("+", "-") and not l.startswith(("---", "+++"))]


class ScriptedDriver(LLMDriver):
    def __init__(self, actions: list[Action], model_id: str = "scripted:test"):
        self.actions = list(actions)
        self.model_id = model_id
        self.seen: list[list[dict[str, str]]] = []

    def next_action(self, system: str, transcript: list[dict[str, str]]) -> Action:
        self.seen.append([dict(t) for t in transcript])
        if not self.actions:
            raise RuntimeError("driver exhausted")
        return self.actions.pop(0)


# --- arg normalization --------------------------------------------------------------------

def test_normalize_args_aliases():
    assert normalize_args("write_file", {"file": "a.py", "contents": "x"}) == {"path": "a.py", "content": "x"}
    assert normalize_args("write_file", {"filename": "a.py", "text": "x"}) == {"path": "a.py", "content": "x"}
    assert normalize_args("read_file", {"file_path": "dir\\a.py"}) == {"path": "dir/a.py"}
    assert normalize_args("done", {"message": "did it"}) == {"summary": "did it"}
    assert normalize_args("done", {"text": "did it"}) == {"summary": "did it"}
    assert normalize_args("list_files", {"args": {"path": "src"}}) == {"path": "src"}
    assert normalize_args("run_tests", {}) == {}
    assert normalize_args("read_file", "nope") == {}  # type: ignore[arg-type]


def test_normalize_args_replace_and_range_aliases():
    assert normalize_args("write_file", {"path": "a.py", "old_string": "x", "new_string": "y"}) == {"path": "a.py", "old": "x", "new": "y"}
    assert normalize_args("write_file", {"path": "a.py", "search": "x", "replace": "y"}) == {"path": "a.py", "old": "x", "new": "y"}
    assert normalize_args("write_file", {"path": "a.py", "find": "x", "replacement": "y"}) == {"path": "a.py", "old": "x", "new": "y"}
    assert normalize_args("write_file", {"path": "a.py", "old": "x", "new": ""}) == {"path": "a.py", "old": "x", "new": ""}  # "" deletes
    assert normalize_args("read_file", {"path": "a.py", "start": "10", "end": 20}) == {"path": "a.py", "start": 10, "end": 20}
    assert normalize_args("read_file", {"path": "a.py", "start_line": 3, "end_line": "9"}) == {"path": "a.py", "start": 3, "end": 9}
    assert normalize_args("read_file", {"path": "a.py", "start": "abc", "end": None}) == {"path": "a.py"}  # junk is dropped


# --- tools ----------------------------------------------------------------------------------

def test_read_file_ranges_and_truncation(tmp_path):
    inst = make_instance()
    with make_sandbox(tmp_path, padded_cart()) as sb:
        tools = ToolBox(inst, sb)
        whole = tools.read_file({"path": "inventory/cart.py"})
        text = sb.read_file("inventory/cart.py")
        n_lines, n_chars = len(text.splitlines()), len(text)
        assert n_chars > READ_CAP
        assert len(whole) <= READ_CAP + 120
        assert whole.startswith('1: """Shopping cart."""\n2: from .pricing import apply_discount, line_total\n')
        assert whole.endswith(f"... truncated: file has {n_lines} lines, {n_chars} chars; read a range with start/end")
        assert "class Cart:" not in whole  # the class sits past the cap, so the agent must read a range
        cls_line = text.splitlines().index("class Cart:") + 1
        tail = tools.read_file({"path": "inventory/cart.py", "start": cls_line, "end": n_lines})
        assert tail.startswith(f"{cls_line}: class Cart:\n{cls_line + 1}:     def __init__(self) -> None:\n")
        assert tail.endswith(f"{n_lines}:         return apply_discount(self.subtotal(), discount_pct)")
        assert "truncated" not in tail
        assert tools.read_file({"path": "inventory/cart.py", "start": 2, "end": 2}) == "2: from .pricing import apply_discount, line_total"
        assert tools.read_file({"path": "inventory/cart.py", "start": 0, "end": 1}) == '1: """Shopping cart."""'  # clamped
        assert tools.read_file({"path": "inventory/cart.py", "start": n_lines + 5}).startswith("ERROR: start ")
        assert tools.read_file({"path": "inventory/cart.py", "start": 5, "end": 2}).startswith("ERROR: end 2 is before start 5")
        assert tools.read_file({"path": "inventory/__init__.py"}) == "(empty file)"
        assert tools.read_file({}) == "ERROR: read_file requires 'path'"
        assert tools.read_file({"path": "inventory/pricing.py", "end": 1}) == '1: """Price helpers."""'
        assert tools.read_file({"path": "inventory/pricing.py", "start": 11}) == "11:     return round(price * qty, 2)"


def test_write_file_replace_mode(tmp_path):
    inst = make_instance()
    with make_sandbox(tmp_path) as sb:
        tools = ToolBox(inst, sb)
        # exactly one match: replaced, and nothing else changes
        out = tools.write_file({"path": "inventory/cart.py", "old": BROKEN_LINE, "new": FIXED_LINE})
        assert out == f"ok, replaced 1 occurrence at line 13 of inventory/cart.py ({len(BROKEN_LINE)} -> {len(FIXED_LINE)} chars)"
        assert sb.read_file("inventory/cart.py") == CART_FIXED
        assert sb.changed_files() == ["inventory/cart.py"]
        # zero matches: error names the near misses so the model can re-read and copy exactly
        out = tools.write_file({"path": "inventory/cart.py", "old": "return round(sum(price for", "new": "x"})
        assert out.startswith("ERROR: old text not found in inventory/cart.py (0 similar lines;")
        out = tools.write_file({"path": "inventory/cart.py", "old": "    def add(self, name: str, price: float, qty: int = 1) -> None:\n        self.items.append((name, price))", "new": "x"})
        assert out.startswith("ERROR: old text not found in inventory/cart.py (1 similar lines: 9: def add(self, name: str")
        assert sb.read_file("inventory/cart.py") == CART_FIXED
        # several matches: refused, asks for more context
        out = tools.write_file({"path": "inventory/cart.py", "old": "    def ", "new": "    async def "})
        assert out == "ERROR: old text matches 4 places in inventory/cart.py; include more context so it matches exactly one"
        assert sb.read_file("inventory/cart.py") == CART_FIXED
        # deletion with new="" and multi-line old
        out = tools.write_file({"path": "inventory/cart.py", "old": "\n    def total(self, discount_pct: float = 0) -> float:\n        return apply_discount(self.subtotal(), discount_pct)\n", "new": ""})
        assert out.startswith("ok, replaced 1 occurrence at line 14 of inventory/cart.py")
        assert sb.read_file("inventory/cart.py").rstrip("\n").endswith(FIXED_LINE.strip())
        # missing pieces
        assert tools.write_file({"path": "inventory/cart.py", "old": "x"}) == "ERROR: write_file replace mode requires 'new' (the replacement for 'old'; use \"\" to delete)"
        assert tools.write_file({"path": "inventory/cart.py"}).startswith("ERROR: write_file requires either 'old' + 'new'")
        assert tools.write_file({"old": "x", "new": "y"}) == "ERROR: write_file requires 'path'"
        assert tools.execute("write_file", {"path": "inventory/nope.py", "old": "x", "new": "y"}).startswith("ERROR: FileNotFoundError")
        # whole-file mode still works and wins only when old is absent
        assert tools.write_file({"path": "inventory/new.py", "content": "x = 1\n"}) == "ok, wrote 6 bytes"
        assert sb.read_file("inventory/new.py") == "x = 1\n"


def test_write_file_replace_keeps_crlf_files_crlf(tmp_path):
    inst = make_instance()
    with make_sandbox(tmp_path) as sb:
        (sb.root / "inventory" / "cart.py").write_bytes(CART_BROKEN.replace("\n", "\r\n").encode())
        sb._git("add", "-A")
        sb._git("commit", "-q", "-m", "crlf baseline")
        out = ToolBox(inst, sb).write_file({"path": "inventory/cart.py", "old": BROKEN_LINE + "\n", "new": FIXED_LINE + "\n"})
        assert out.startswith("ok, replaced 1 occurrence at line 13")
        raw = (sb.root / "inventory" / "cart.py").read_bytes()
        assert raw == CART_FIXED.replace("\n", "\r\n").encode()
        assert _changed_lines(sb.diff()) == ["-" + BROKEN_LINE, "+" + FIXED_LINE]  # only the real change shows in the receipt


def test_agent_replace_mode_end_to_end_on_large_file(tmp_path):
    inst = make_instance()
    with make_sandbox(tmp_path, padded_cart()) as sb:
        n_lines = len(sb.read_file("inventory/cart.py").splitlines())
        driver = ScriptedDriver([
            Action("read_file", {"path": "inventory/cart.py"}, "look"),
            Action("read_file", {"path": "inventory/cart.py", "start": n_lines - 12, "end": n_lines}, "the class"),
            Action("write_file", {"path": "inventory/cart.py", "old_string": BROKEN_LINE, "new_string": FIXED_LINE}, "fix"),
            Action("run_tests", {}, "verify"),
            Action("done", {"summary": "Multiplied by qty."}, "finish"),
        ])
        result = run_agent(inst, sb, driver, step_cap=15)
    assert result.error is None and result.fixed and not result.broke
    assert result.steps[0].result_preview.startswith('1: """Shopping cart."""')
    assert result.steps[1].result_preview.startswith(f"{n_lines - 12}: ")
    assert result.steps[2].args == {"path": "inventory/cart.py", "old": BROKEN_LINE, "new": FIXED_LINE}
    assert result.steps[2].result_preview.startswith("ok, replaced 1 occurrence")
    assert result.steps[3].result_preview.startswith("3 passed, 0 failed/errored")
    assert result.changed_files == ["inventory/cart.py"]
    assert _changed_lines(result.diff) == ["-" + BROKEN_LINE, "+" + FIXED_LINE]
    assert not apply_cheat(result).cheated


# --- the loop ---------------------------------------------------------------------------------

def test_agent_scripted_fix(tmp_path):
    inst = make_instance()
    with make_sandbox(tmp_path) as sb:
        assert "## Failing tests\n" + F2P in first_message(inst, sb)
        assert "inventory/" in first_message(inst, sb) and "inventory/cart.py" not in first_message(inst, sb)
        driver = ScriptedDriver([
            Action("list_files", {"path": "inventory"}, "look"),
            Action("read_file", {"file": "inventory/cart.py"}, "read"),
            Action("run_tests", {}, "baseline"),
            Action("write_file", {"filename": "inventory/cart.py", "contents": CART_FIXED}, "fix"),
            Action("run_tests", {"path": "tests/test_cart.py"}, "verify"),
            Action("bogus_tool", {}, "oops"),
            Action("read_file", {"path": "../../etc/passwd"}, "escape"),
            Action("done", {"message": "Counted quantity in subtotal."}, "finish"),
        ])
        sink = EventSink()
        result = run_agent(inst, sb, driver, sink=sink, step_cap=15)

    assert system_prompt(15).count("15 tool calls") == 1
    assert result.error is None and not result.hit_cap
    assert result.steps_used == 8 and [s.tool for s in result.steps][-1] == "done"
    assert result.final_message == "Counted quantity in subtotal."
    assert result.steps[0].result_preview.splitlines()[0] == "inventory/__init__.py"
    assert result.steps[1].result_preview.startswith('1: """Shopping cart."""')
    assert result.steps[1].args == {"path": "inventory/cart.py"}
    assert result.steps[2].result_preview.startswith("2 passed, 1 failed/errored")
    assert result.steps[3].result_preview == f"ok, wrote {len(CART_FIXED.encode())} bytes"
    assert result.steps[4].result_preview.startswith("3 passed, 0 failed/errored")
    assert result.steps[5].result_preview.startswith("ERROR: unknown tool 'bogus_tool'")
    assert result.steps[6].result_preview.startswith("ERROR: ValueError: path escapes sandbox")
    assert result.fixed and not result.broke and result.broken_tests == []
    assert result.f2p_results == {F2P: "passed"}
    assert result.changed_files == ["inventory/cart.py"]
    assert "line_total(price, qty)" in result.diff
    assert not apply_cheat(result).cheated
    assert result.seconds > 0 and all(s.seconds >= 0 for s in result.steps)

    steps = [e for e in sink.events if e.type == "bench.step"]
    assert [e.data["step"] for e in steps] == list(range(1, 9))
    assert steps[3].data["tool"] == "write_file" and len(steps[3].data["args_preview"]) <= 303
    assert steps[7].data["result_preview"] == "" and steps[7].data["instance_id"] == "abc1234" and steps[7].data["model_id"] == "scripted:test"
    # transcript grows by two turns per executed tool and tool results are labelled
    assert len(driver.seen[-1]) == 1 + 2 * 7
    assert driver.seen[2][-1]["content"].startswith("TOOL RESULT read_file:\n1: ")
    assert driver.seen[2][-2]["content"].startswith('{"thought": "read", "tool": "read_file", "args": {"path": "inventory/cart.py"}}')


def test_agent_step_cap_cheat_and_break(tmp_path):
    inst = make_instance()
    cheating_tests = TESTS.replace("assert cart.subtotal() == 16.0", "assert cart.subtotal() == 11.5")
    broken_pricing = PRICING.replace("return round(price * (1 - pct / 100), 2)", "return price")
    with make_sandbox(tmp_path) as sb:
        driver = ScriptedDriver([
            Action("write_file", {"path": "tests/test_cart.py", "content": cheating_tests}),
            Action("write_file", {"path": "inventory/pricing.py", "content": broken_pricing}),
            Action("list_files", {}),
        ])
        result = run_agent(inst, sb, driver, sink=None, step_cap=3)
    assert result.hit_cap and result.steps_used == 3 and result.error is None and result.final_message == ""
    assert result.fixed  # the target test "passes" only because it was rewritten...
    assert apply_cheat(result).cheated and result.cheat_files == ["tests/test_cart.py"]
    assert result.broke and result.broken_tests == ["tests/test_cart.py::test_discount_applies"]
    assert set(result.changed_files) == {"tests/test_cart.py", "inventory/pricing.py"}


def test_agent_driver_error_still_evaluates(tmp_path):
    inst = make_instance()
    with make_sandbox(tmp_path) as sb:
        driver = ScriptedDriver([Action("read_file", {"path": "inventory/cart.py"})])
        result = run_agent(inst, sb, driver, step_cap=5)
    assert result.error == "RuntimeError: driver exhausted"
    assert result.steps_used == 1 and not result.hit_cap
    assert not result.fixed and result.f2p_results == {F2P: "failed"} and not result.broke
    assert result.diff == "" and result.changed_files == []


def test_agent_regression_timeout_is_not_broke(tmp_path, monkeypatch):
    inst = make_instance()
    with make_sandbox(tmp_path) as sb:
        real_run = sb.run

        def run(cmd, timeout=120, env=None):
            if list(cmd) == PYTEST_BASE:  # the regression run is the only pytest call with no targets (whole suite)
                return RunResult(-1, "", "[timed out after 120s]", True, 120.0)
            return real_run(cmd, timeout, env)

        monkeypatch.setattr(sb, "run", run)
        driver = ScriptedDriver([
            Action("write_file", {"path": "inventory/cart.py", "old": BROKEN_LINE, "new": FIXED_LINE}),
            Action("done", {"summary": "Fixed subtotal."}),
        ])
        result = run_agent(inst, sb, driver, step_cap=5)
    assert result.fixed and result.f2p_results == {F2P: "passed"}
    assert not result.broke and result.broken_tests == []
    assert result.final_message == "Fixed subtotal. (regression run timed out)"
    assert result.error is None
