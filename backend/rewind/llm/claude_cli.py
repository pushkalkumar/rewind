"""Drive the local Claude Code CLI as a plain LLM. No API key: `claude -p` with all tools disabled and a JSON schema.

One subprocess per step. The whole transcript is rendered into a single user message (stream-json on stdin);
the system prompt travels on argv. The CLI refuses to run nested inside Claude Code, so CLAUDECODE is stripped.

The action normally arrives as `structured_output` (the CLI's StructuredOutput tool). The schema handed to the
CLI is FLAT: {tool, thought, path, content, old, new, summary, start, end} with no nested "args" object, because
models given a nested schema kept putting the whole action under "args" (or the arguments beside "tool"), the
CLI rejected it against the schema, and with --max-turns 1 the step died or came back with empty arguments.
Every recovery path lifts the innermost object that carries "tool" and merges the argument keys from every
level, so a nested answer still yields a complete action. Smaller models sometimes answer with the JSON as
plain text, or emit a native tool_use block named after one of our tools; both are recovered from the assistant
messages in the stream so a step is not wasted.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Optional

from ..config import LLM_TIMEOUT
from . import ARG_PROPERTIES, TOOL_NAMES, Action, LLMDriver, parse_action

MAX_SYSTEM_CHARS = 20_000
MAX_TURNS = 1  # a second turn lets the CLI nudge for StructuredOutput, but small models then "finish" a role-played session
WRAPPER_KEYS = ("args", "action", "input", "arguments", "parameters")
ARG_DESCRIPTIONS = {
    "path": "Repo-relative path (list_files, read_file, write_file, run_tests).",
    "content": "write_file whole-file mode: the complete new file text.",
    "old": "write_file replace mode: exact text to find (must match exactly one place).",
    "new": "write_file replace mode: replacement for `old`.",
    "summary": "What you changed and why, for done.",
    "start": "read_file: first line to show (1-based, inclusive).",
    "end": "read_file: last line to show (1-based, inclusive).",
}
FLAT_SHAPE = (
    '{"thought": "...", "tool": "<name>", "path": "...", "content": "...", "old": "...", "new": "...", '
    '"summary": "...", "start": 1, "end": 1}'
)
TAIL_PROMPT = (
    "### ASSISTANT\nCall StructuredOutput now with your next action as ONE flat object: "
    '"thought", "tool", and the tool arguments as top-level keys (no "args" wrapper).'
)
SYSTEM_SUFFIX = (
    "\n\nOutput format for this session (this replaces the JSON shape shown above): call the StructuredOutput tool "
    "with exactly ONE action and then stop. The object is FLAT: " + FLAT_SHAPE + ' -- include "thought", "tool" '
    'and only the argument keys the tool needs, all at the top level. Do NOT nest the arguments inside an "args" '
    "object. Never write a '### USER' or 'TOOL RESULT' section yourself; the harness runs the tool and sends you the "
    "result next turn."
)


def cli_schema() -> dict[str, Any]:
    """The flat action schema the CLI validates StructuredOutput against: tool + thought + every argument, top level."""
    props: dict[str, Any] = {
        "thought": {"type": "string", "description": "One or two sentences of reasoning."},
        "tool": {"type": "string", "enum": list(TOOL_NAMES)},
    }
    for key, spec in ARG_PROPERTIES.items():
        props[key] = {**spec, "description": ARG_DESCRIPTIONS[key]}
    return {"type": "object", "properties": props, "required": ["tool"]}


def flatten_action_json(content: str) -> str:
    """An assistant turn ({"thought", "tool", "args": {...}}) re-rendered in the flat shape the CLI is asked for."""
    try:
        obj = json.loads(content)
    except ValueError:
        return content
    if not isinstance(obj, dict) or obj.get("tool") not in TOOL_NAMES:
        return content
    flat: dict[str, Any] = {"thought": obj.get("thought", ""), "tool": obj["tool"]}
    args = obj.get("args")
    if isinstance(args, dict):
        flat.update({k: v for k, v in args.items() if k not in flat})
    flat.update({k: v for k, v in obj.items() if k not in flat and k != "args"})
    return json.dumps(flat, ensure_ascii=False)


def render_transcript(transcript: list[dict[str, str]]) -> str:
    """Flatten role/content turns into one text block ending with an assistant cue. Assistant turns are shown flat."""
    parts = []
    for turn in transcript:
        role = "ASSISTANT" if turn.get("role") == "assistant" else "USER"
        content = turn.get("content", "")
        if role == "ASSISTANT":
            content = flatten_action_json(content)
        parts.append(f"### {role}\n{content}\n")
    parts.append(TAIL_PROMPT)
    return "\n".join(parts)


def parse_stream(stdout: str) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """(result event, assistant content blocks in order) from the CLI's stream-json stdout."""
    result: Optional[dict[str, Any]] = None
    blocks: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "result":
            result = d
        elif d.get("type") == "assistant":
            content = (d.get("message") or {}).get("content")
            if isinstance(content, list):
                blocks.extend(b for b in content if isinstance(b, dict))
    return result, blocks


def _collect(obj: dict[str, Any], depth: int = 0) -> tuple[Optional[str], str, dict[str, Any]]:
    """(tool, thought, args) from an action object in any of the shapes models produce.

    Flat: {"tool", "thought", "path", ...}. Nested: {"tool", "args": {...}}. Wrapped: {"args": {"tool", "args": {...}}},
    {"action": {...}}, {"input": {...}}. The innermost object carrying "tool" wins for the tool; argument keys are merged
    from every level (inner beats outer) so nothing the model typed next to the tool name is lost.
    """
    tool = obj.get("tool") if obj.get("tool") in TOOL_NAMES else None
    thought = str(obj.get("thought") or "")
    args: dict[str, Any] = {}
    for k, v in obj.items():
        if k in ("tool", "thought") or k in WRAPPER_KEYS or isinstance(v, (dict, list)) or v in (None, ""):
            continue
        args[k] = v
    if depth < 4:
        for k in WRAPPER_KEYS:
            inner = obj.get(k)
            if not isinstance(inner, dict):
                continue
            inner_tool, inner_thought, inner_args = _collect(inner, depth + 1)
            args.update(inner_args)
            tool = inner_tool or tool
            thought = inner_thought or thought
    return tool, thought, args


def _action_from_obj(obj: dict[str, Any]) -> Optional[Action]:
    if not isinstance(obj, dict):
        return None
    tool, thought, args = _collect(obj)
    if tool is None:
        return None
    return Action(tool=tool, args=args, thought=thought, raw=json.dumps(obj))


def first_action_object(text: str, max_tries: int = 20) -> Optional[dict[str, Any]]:
    """The first complete JSON object in `text` that carries a known tool (ignores anything the model wrote after it)."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    tries = 0
    while start != -1 and tries < max_tries:
        tries += 1
        try:
            obj, _ = decoder.raw_decode(text, start)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and _collect(obj)[0] is not None:
            return obj
        start = text.find("{", start + 1)
    return None


def action_from_blocks(blocks: list[dict[str, Any]]) -> Optional[Action]:
    """Recover an action the model expressed outside structured_output: a tool_use block or JSON in its text."""
    for b in blocks:
        if b.get("type") != "tool_use":
            continue
        name, inp = b.get("name"), b.get("input")
        if name == "StructuredOutput" and isinstance(inp, dict):
            act = _action_from_obj(inp)
            if act:
                return act
        elif name in TOOL_NAMES:
            args = _collect(inp)[2] if isinstance(inp, dict) else {}
            return Action(tool=name, args=args, thought="", raw=json.dumps(b))
    text = "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
    obj = first_action_object(text)
    return _action_from_obj(obj) if obj else None


def action_from_stream(result: dict[str, Any], blocks: list[dict[str, Any]]) -> Optional[Action]:
    so = result.get("structured_output")
    if isinstance(so, dict):
        act = _action_from_obj(so)
        if act:
            return act
    act = action_from_blocks(blocks)
    if act:
        return act
    text = str(result.get("result") or "")
    return parse_action(text) if text and not result.get("is_error") else None


class ClaudeCLIDriver(LLMDriver):
    def __init__(self, model_name: str = "sonnet", timeout: int = LLM_TIMEOUT, exe: Optional[str] = None):
        self.model_name = model_name or "sonnet"
        self.model_id = f"claude-cli:{self.model_name}"
        self.timeout = timeout
        self.exe = exe or shutil.which("claude")
        if not self.exe:
            raise RuntimeError("claude CLI not found on PATH (install Claude Code or pick another model provider)")
        self.cwd = tempfile.gettempdir()

    def build_argv(self, system: str) -> list[str]:
        return [
            self.exe, "-p", "--model", self.model_name, "--no-session-persistence", "--tools", "",
            "--system-prompt", (system + SYSTEM_SUFFIX)[:MAX_SYSTEM_CHARS],
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--max-turns", str(MAX_TURNS), "--json-schema", json.dumps(cli_schema()),
        ]

    @staticmethod
    def build_stdin(transcript: list[dict[str, str]]) -> str:
        msg = {"type": "user", "message": {"role": "user", "content": render_transcript(transcript)}}
        return json.dumps(msg) + "\n"

    @staticmethod
    def build_env() -> dict[str, str]:
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _invoke(self, system: str, transcript: list[dict[str, str]]) -> Action:
        res = subprocess.run(
            self.build_argv(system), input=self.build_stdin(transcript).encode("utf-8"), capture_output=True,
            env=self.build_env(), cwd=self.cwd, timeout=self.timeout,
        )
        stdout = res.stdout.decode("utf-8", errors="replace")
        stderr = res.stderr.decode("utf-8", errors="replace")
        result, blocks = parse_stream(stdout)
        if result is None:
            raise RuntimeError(f"claude CLI produced no result (rc={res.returncode}): {(stderr or stdout)[-800:]}")
        action = action_from_stream(result, blocks)
        if action is None:
            errors = result.get("errors") or [result.get("result") or result.get("subtype") or "no action"]
            raise RuntimeError(f"claude CLI error: {'; '.join(str(e) for e in errors)[:400]} {stderr[-400:]}".rstrip())
        return action

    def next_action(self, system: str, transcript: list[dict[str, str]]) -> Action:
        last: Exception = RuntimeError("unreachable")
        for _ in range(2):
            try:
                return self._invoke(system, transcript)
            except subprocess.TimeoutExpired as e:
                tail = (e.stderr or b"")[-400:] if isinstance(e.stderr, bytes) else str(e.stderr or "")[-400:]
                last = RuntimeError(f"claude CLI timed out after {self.timeout}s: {tail}")
            except RuntimeError as e:
                last = e
        raise last
