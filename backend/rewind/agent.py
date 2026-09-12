"""The agent loop: (Instance, Sandbox, LLMDriver) -> AgentResult, before scoring.

Each step the driver picks one action; the loop executes it in the sandbox, appends the JSON action and the
tool result to the transcript, and reports a `bench.step` event. When the episode ends (done, step cap, or a
driver error) the loop always collects the diff and re-runs the fail-to-pass and pass-to-pass tests.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from .config import STEP_CAP, TEST_TIMEOUT, model_label
from .events import EventSink
from .llm import Action, LLMDriver
from .models import AgentResult, Instance, ToolCall
from .pytest_runner import run_pytest, summarize
from .sandbox import Sandbox

READ_CAP = 60_000
SIMILAR_LINES_SHOWN = 3
LIST_CAP = 300
TEST_OUTPUT_CAP = 3000
PREVIEW_CHARS = 400
TOP_LEVEL_ENTRIES = 60

SYSTEM_PROMPT = """You are an autonomous software engineer fixing a bug in a Python repository.

You work by calling tools, one per turn. Respond ONLY with a JSON object:
{"thought": "<one or two sentences>", "tool": "<name>", "args": {...}}
Emit exactly one action and stop. The harness executes it and replies with "TOOL RESULT <tool>:"; never write that part yourself.

Tools:
- list_files {"path": "."}          List files under a directory (recursive). Directories end with "/".
- read_file {"path": "<file>", "start": <line>, "end": <line>}
                                    Read a file; lines come back prefixed "N: ". start/end are optional 1-based
                                    inclusive line numbers for reading just a range (large files are truncated).
- write_file {"path": "<file>", "old": "<exact existing text>", "new": "<replacement text>"}
                                    Replace mode: `old` must match exactly ONE place in the file (copy it verbatim
                                    from read_file output without the "N: " prefixes, including indentation); it is
                                    replaced by `new`. Preferred for edits: send only the lines that change.
- write_file {"path": "<file>", "content": "<full text>"}
                                    Whole-file mode: replace the entire file with `content` (creates it if missing).
- run_tests {"path": "<optional>"}  Run pytest. Defaults to the failing test files; pass a path to run something else.
- done {"summary": "<what you changed and why>"}
                                    End the episode.

Rules:
- Read a file before you write it. Prefer write_file with old/new; only use content when you mean to replace the whole file, and then send the complete new text.
- You have at most {step_cap} tool calls in total, including the final done. Budget them.
- The test files describe the expected behavior. Fix the underlying bug in the source code so the failing tests pass.
- Finish with done as soon as run_tests shows the target tests passing.
"""

ARG_ALIASES: dict[str, tuple[str, ...]] = {
    "path": ("file", "filename", "filepath", "file_path", "target", "name", "dir", "directory"),
    "content": ("contents", "text", "body", "data", "new_content", "source", "code"),
    "old": ("old_string", "old_str", "old_text", "search", "find", "target_text", "original"),
    "new": ("new_string", "new_str", "new_text", "replace", "replacement", "replace_with"),
    "summary": ("message", "result", "description", "text", "note"),
    "start": ("start_line", "from_line", "line_start", "first_line", "offset", "from"),
    "end": ("end_line", "to_line", "line_end", "last_line", "to"),
}
TOOL_KEYS: dict[str, tuple[str, ...]] = {
    "list_files": ("path",),
    "read_file": ("path", "start", "end"),
    "write_file": ("path", "content", "old", "new"),
    "run_tests": ("path",),
    "done": ("summary",),
}
INT_KEYS = ("start", "end")


def system_prompt(step_cap: int = STEP_CAP) -> str:
    return SYSTEM_PROMPT.replace("{step_cap}", str(step_cap))


def normalize_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Map the key names models like to invent onto the contract's names."""
    if not isinstance(args, dict):
        return {}
    for wrapper in ("args", "arguments", "input", "parameters"):
        inner = args.get(wrapper)
        if isinstance(inner, dict) and len(args) == 1:
            args = inner
    out = dict(args)
    for key in TOOL_KEYS.get(tool, ()):
        if out.get(key) not in (None, ""):
            continue
        for alias in ARG_ALIASES.get(key, ()):
            if alias in out and out[alias] not in (None, "") and alias not in TOOL_KEYS.get(tool, ()):
                out[key] = out.pop(alias)
                break
    for key in ("path", "content", "old", "new", "summary"):
        if key in out and out[key] is not None and not isinstance(out[key], str):
            out[key] = str(out[key])
    for key in INT_KEYS:
        if key in out:
            n = _as_int(out.pop(key))
            if n is not None:
                out[key] = n
    if "path" in out and isinstance(out["path"], str):
        out["path"] = out["path"].strip().strip("\"'").strip().replace("\\", "/") or "."
    return out


def _as_int(value: Any) -> Optional[int]:
    """start/end arrive as ints, numeric strings, or junk; junk is dropped so the tool falls back to the whole file."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _numbered(text: str, start: Optional[int] = None, end: Optional[int] = None) -> str:
    """Line-numbered file text. `start`/`end` are 1-based inclusive; anything beyond READ_CAP is cut at a line boundary."""
    lines = text.splitlines()
    if not lines:
        return "(empty file)"
    first = max(1, start or 1)
    last = min(len(lines), end) if end is not None else len(lines)
    if first > len(lines):
        return f"ERROR: start {first} is past the end of the file ({len(lines)} lines)"
    if last < first:
        return f"ERROR: end {last} is before start {first} (file has {len(lines)} lines)"
    body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(first, last + 1))
    if len(body) > READ_CAP:
        cut = body.rfind("\n", 0, READ_CAP)
        body = body[: cut if cut > 0 else READ_CAP]
        body += f"\n... truncated: file has {len(lines)} lines, {len(text)} chars; read a range with start/end"
    return body


def top_level_listing(sandbox: Sandbox) -> str:
    entries = [e for e in sandbox.list_files(".", max_entries=2000) if "/" not in e.rstrip("/") and not e.startswith("...")]
    shown = entries[:TOP_LEVEL_ENTRIES]
    if len(entries) > TOP_LEVEL_ENTRIES:
        shown.append(f"... {len(entries) - TOP_LEVEL_ENTRIES} more")
    return "\n".join(shown)


def first_message(instance: Instance, sandbox: Sandbox) -> str:
    return (
        f"## Issue\n{instance.issue_text.strip()}\n\n"
        f"## Failing tests\n" + "\n".join(instance.fail_to_pass) + "\n\n"
        f"## Repository (top level)\n{top_level_listing(sandbox)}"
    )


class ToolBox:
    """Executes the contract's tools against a sandbox; every failure becomes an "ERROR: ..." string."""

    def __init__(self, instance: Instance, sandbox: Sandbox):
        self.instance = instance
        self.sandbox = sandbox

    def list_files(self, args: dict[str, Any]) -> str:
        entries = self.sandbox.list_files(args.get("path") or ".", max_entries=LIST_CAP)
        return "\n".join(entries) if entries else "(empty directory)"

    def read_file(self, args: dict[str, Any]) -> str:
        path = args.get("path")
        if not path:
            return "ERROR: read_file requires 'path'"
        return _numbered(self.sandbox.read_file(path), args.get("start"), args.get("end"))

    def write_file(self, args: dict[str, Any]) -> str:
        path, content, old, new = args.get("path"), args.get("content"), args.get("old"), args.get("new")
        if not path:
            return "ERROR: write_file requires 'path'"
        if old:
            if new is None:
                return "ERROR: write_file replace mode requires 'new' (the replacement for 'old'; use \"\" to delete)"
            return self._replace(path, old, new)
        if content is None:
            return "ERROR: write_file requires either 'old' + 'new' (replace one occurrence) or 'content' (the full new file text)"
        self.sandbox.write_file(path, content)
        return f"ok, wrote {len(content.encode('utf-8'))} bytes"

    def _replace(self, path: str, old: str, new: str) -> str:
        text = self.sandbox.read_file(path)
        if "\r\n" in text and "\r" not in old:  # backends that hand back CRLF text verbatim
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        count = text.count(old)
        if count == 0:
            return f"ERROR: old text not found in {path} ({_similar_lines(text, old)})"
        if count > 1:
            return f"ERROR: old text matches {count} places in {path}; include more context so it matches exactly one"
        line = text[: text.index(old)].count("\n") + 1
        self.sandbox.write_file(path, text.replace(old, new, 1))
        return f"ok, replaced 1 occurrence at line {line} of {path} ({len(old)} -> {len(new)} chars)"

    def run_tests(self, args: dict[str, Any]) -> str:
        path = args.get("path")
        targets = [path] if path and path != "." else list(self.instance.test_files)
        run = run_pytest(self.sandbox.run, targets, timeout=TEST_TIMEOUT)
        text = summarize(run, limit=TEST_OUTPUT_CAP)
        if not run.tests and run.returncode != 0:  # collection/import error: show the agent why nothing ran
            text = (text + "\n--- pytest output (tail) ---\n" + run.stdout_tail)[-TEST_OUTPUT_CAP:]
        return text

    def execute(self, tool: str, args: dict[str, Any]) -> str:
        fn: Optional[Callable[[dict[str, Any]], str]] = getattr(self, tool, None) if tool in TOOL_KEYS else None
        if fn is None:
            return f"ERROR: unknown tool {tool!r}. Use one of: list_files, read_file, write_file, run_tests, done"
        try:
            return fn(args)
        except Exception as e:  # tools never crash the loop
            return f"ERROR: {type(e).__name__}: {e}"


def _similar_lines(text: str, old: str) -> str:
    """Hint for a failed replace: the lines that look like the first line of `old`, so the model can re-read and retry."""
    probe = next((l.strip() for l in old.splitlines() if l.strip()), "")
    hits = [(i + 1, l) for i, l in enumerate(text.splitlines()) if probe and probe in l] if probe else []
    if not hits:
        return "0 similar lines; read the file again and copy the text exactly, without the line-number prefixes"
    shown = ", ".join(f"{n}: {l.strip()[:80]}" for n, l in hits[:SIMILAR_LINES_SHOWN])
    return f"{len(hits)} similar lines: {shown}; the rest of the old text differs, re-read those lines and copy them exactly"


def _preview(text: str, n: int = PREVIEW_CHARS) -> str:
    return text if len(text) <= n else text[:n] + "..."


def _args_preview(args: dict[str, Any]) -> str:
    short = {k: (_preview(v, 120) if isinstance(v, str) else v) for k, v in args.items()}
    return _preview(json.dumps(short, ensure_ascii=False), 300)


def _assistant_turn(action: Action, args: dict[str, Any]) -> str:
    return json.dumps({"thought": action.thought, "tool": action.tool, "args": args}, ensure_ascii=False)


def _emit(sink: Optional[EventSink], type: str, **data: Any) -> None:
    if sink is not None:
        sink.emit(type, **data)


def _f2p_results(instance: Instance, sandbox: Sandbox) -> dict[str, str]:
    if not instance.fail_to_pass:
        return {}
    run = run_pytest(sandbox.run, list(instance.test_files), node_ids=list(instance.fail_to_pass), timeout=TEST_TIMEOUT)
    seen = {t.nodeid: t.status for t in run.tests}
    return {nid: seen.get(nid, "error") for nid in instance.fail_to_pass}


def _regression(instance: Instance, sandbox: Sandbox) -> tuple[list[str], bool]:
    """(pass_to_pass tests that no longer pass, whether the run timed out). A timed-out run proves nothing."""
    if not instance.pass_to_pass:
        return [], False
    targets = list(getattr(instance, "regression_targets", None) or [])
    run = run_pytest(sandbox.run, targets, timeout=TEST_TIMEOUT)
    if run.timed_out:
        return [], True
    passed = run.passed
    return [p for p in instance.pass_to_pass if p not in passed], False


def run_agent(instance: Instance, sandbox: Sandbox, driver: LLMDriver, sink: Optional[EventSink] = None, step_cap: int = STEP_CAP) -> AgentResult:
    t0 = time.time()
    model_id = getattr(driver, "model_id", "") or "unknown"
    result = AgentResult(instance_id=instance.id, model_id=model_id, model_label=model_label(model_id))
    system = system_prompt(step_cap)
    tools = ToolBox(instance, sandbox)
    transcript: list[dict[str, str]] = [{"role": "user", "content": first_message(instance, sandbox)}]

    step = 0
    finished = False
    while step < step_cap and not finished:
        step += 1
        ts = time.time()
        try:
            action = driver.next_action(system, transcript)
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
            step -= 1
            break
        args = normalize_args(action.tool, action.args)
        if action.tool == "done":
            output = ""
            result.final_message = str(args.get("summary") or action.thought or "")
            finished = True
        else:
            output = tools.execute(action.tool, args)
            transcript.append({"role": "assistant", "content": _assistant_turn(action, args)})
            transcript.append({"role": "user", "content": f"TOOL RESULT {action.tool}:\n{output}"})
        call = ToolCall(step=step, tool=action.tool, args=args, result_preview=_preview(output), seconds=round(time.time() - ts, 2), thought=action.thought)
        result.steps.append(call)
        _emit(sink, "bench.step", instance_id=instance.id, model_id=model_id, step=step, tool=action.tool,
              args_preview=_args_preview(args), result_preview=call.result_preview)
    result.steps_used = step
    result.hit_cap = not finished and result.error is None and step >= step_cap

    try:
        result.diff = sandbox.diff()
        result.changed_files = sandbox.changed_files()
        result.f2p_results = _f2p_results(instance, sandbox)  # type: ignore[assignment]
        result.fixed = bool(result.f2p_results) and all(s == "passed" for s in result.f2p_results.values())
        result.broken_tests, regression_timed_out = _regression(instance, sandbox)
        result.broke = bool(result.broken_tests)
        if regression_timed_out:
            result.final_message += " (regression run timed out)"
    except Exception as e:
        result.error = (result.error + "; " if result.error else "") + f"post-run evaluation failed: {type(e).__name__}: {e}"
    result.seconds = round(time.time() - t0, 2)
    return result
