"""LLM drivers: exact request shapes with fake clients, subprocess shape for the CLI, and one real CLI call."""
from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from rewind.llm import ACTION_SCHEMA, TOOL_NAMES, create_driver, parse_action
from rewind.llm.anthropic_api import ACT_TOOL, AnthropicDriver, merge_turns
from rewind.llm.claude_cli import SYSTEM_SUFFIX, TAIL_PROMPT, ClaudeCLIDriver, action_from_stream, cli_schema, first_action_object, flatten_action_json, parse_stream, render_transcript
from rewind.llm.openai_compat import LILAC_DEFAULT_BASE_URL, OpenAICompatDriver, provider_settings

SYSTEM = "You fix bugs."
TRANSCRIPT = [
    {"role": "user", "content": "## Issue\nadd() subtracts"},
    {"role": "assistant", "content": '{"tool": "read_file", "args": {"path": "pkg/calc.py"}}'},
    {"role": "user", "content": "TOOL RESULT read_file:\n1: def add(a, b):\n2:     return a - b"},
]


# --- shared helpers ----------------------------------------------------------------

def test_merge_turns_merges_same_role_and_starts_with_user():
    merged = merge_turns([
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a3"},
    ])
    assert merged == [
        {"role": "user", "content": "Begin."},
        {"role": "assistant", "content": "a1\n\na2"},
        {"role": "user", "content": "u1\n\nu2"},
        {"role": "assistant", "content": "a3"},
    ]
    assert merge_turns([]) == [{"role": "user", "content": "Begin."}]


# --- anthropic ------------------------------------------------------------------------

class FakeAnthropicClient:
    def __init__(self, responses: list[Any]):
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _tool_use(inp: dict[str, Any]) -> Any:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="thinking..."), SimpleNamespace(type="tool_use", name="act", input=inp, id="tu_1")], stop_reason="tool_use")


def test_anthropic_request_shape_and_parsing():
    client = FakeAnthropicClient([_tool_use({"thought": "fix the sign", "tool": "write_file", "args": {"path": "pkg/calc.py", "content": "def add(a, b):\n    return a + b\n"}})])
    driver = AnthropicDriver("claude-sonnet-5", client=client)
    assert driver.model_id == "anthropic:claude-sonnet-5"
    action = driver.next_action(SYSTEM, TRANSCRIPT)
    assert len(client.calls) == 1
    req = client.calls[0]
    assert req == {
        "model": "claude-sonnet-5",
        "max_tokens": 8192,
        "system": SYSTEM,
        "messages": TRANSCRIPT,
        "tools": [ACT_TOOL],
        "tool_choice": {"type": "tool", "name": "act"},
    }
    assert ACT_TOOL["input_schema"] is ACTION_SCHEMA
    assert action.tool == "write_file"
    assert action.args == {"path": "pkg/calc.py", "content": "def add(a, b):\n    return a + b\n"}
    assert action.thought == "fix the sign"
    assert json.loads(action.raw)["tool"] == "write_file"


def test_anthropic_default_model_and_retry_once():
    import anthropic

    err = anthropic.APIStatusError("boom", response=SimpleNamespace(status_code=529, headers={}, request=None), body=None)
    client = FakeAnthropicClient([err, _tool_use({"tool": "done", "args": {"summary": "ok"}})])
    driver = AnthropicDriver("", client=client)
    assert driver.model_name == "claude-sonnet-5"
    action = driver.next_action(SYSTEM, TRANSCRIPT)
    assert action.tool == "done" and action.args == {"summary": "ok"}
    assert len(client.calls) == 2

    client = FakeAnthropicClient([err, err])
    with pytest.raises(RuntimeError, match="after retry"):
        AnthropicDriver("claude-sonnet-5", client=client).next_action(SYSTEM, TRANSCRIPT)
    assert len(client.calls) == 2


def test_anthropic_text_fallback_and_unknown_tool():
    text_only = SimpleNamespace(content=[SimpleNamespace(type="text", text='```json\n{"tool": "run_tests", "args": {}}\n```')])
    driver = AnthropicDriver("claude-sonnet-5", client=FakeAnthropicClient([text_only]))
    assert driver.next_action(SYSTEM, TRANSCRIPT).tool == "run_tests"
    driver = AnthropicDriver("claude-sonnet-5", client=FakeAnthropicClient([_tool_use({"tool": "rm_rf", "args": {}})]))
    action = driver.next_action(SYSTEM, TRANSCRIPT)
    assert action.tool == "done" and "unparseable" in action.args["summary"]


# --- openai-compatible --------------------------------------------------------------

class FakeOpenAIClient:
    def __init__(self, responses: list[Any]):
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _chat_tool_call(inp: dict[str, Any]) -> Any:
    fn = SimpleNamespace(name="act", arguments=json.dumps(inp))
    msg = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="call_1", type="function", function=fn)])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")])


def _chat_text(text: str) -> Any:
    msg = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


def test_openai_request_shape_and_parsing():
    client = FakeOpenAIClient([_chat_tool_call({"thought": "look around", "tool": "list_files", "args": {"path": "src"}})])
    driver = OpenAICompatDriver("gpt-4.1-mini", provider="openai", client=client)
    assert driver.model_id == "openai:gpt-4.1-mini"
    action = driver.next_action(SYSTEM, TRANSCRIPT)
    req = client.calls[0]
    assert req == {
        "model": "gpt-4.1-mini",
        "messages": [{"role": "system", "content": SYSTEM}] + TRANSCRIPT,
        "tools": [{"type": "function", "function": {"name": "act", "description": "Choose the next tool call. Exactly one action per turn.", "parameters": ACTION_SCHEMA}}],
        "tool_choice": {"type": "function", "function": {"name": "act"}},
    }
    assert action.tool == "list_files" and action.args == {"path": "src"} and action.thought == "look around"


def test_openai_falls_back_to_json_mode_on_400():
    import openai

    bad = openai.BadRequestError("tool_choice not supported", response=SimpleNamespace(status_code=400, headers={}, request=None), body=None)
    client = FakeOpenAIClient([bad, _chat_text('{"thought": "t", "tool": "read_file", "args": {"path": "a.py"}}')])
    driver = OpenAICompatDriver("kimi-k2.6", provider="lilac", client=client)
    assert driver.model_id == "lilac:kimi-k2.6"
    action = driver.next_action(SYSTEM, TRANSCRIPT)
    assert action.tool == "read_file" and action.args == {"path": "a.py"}
    assert len(client.calls) == 2
    second = client.calls[1]
    assert "tools" not in second and "tool_choice" not in second
    assert second["response_format"] == {"type": "json_object"}
    assert second["messages"][0]["role"] == "system" and "JSON object" in second["messages"][0]["content"]
    assert driver.json_mode is True
    # the mode sticks: the next request goes straight to json mode
    client._responses.append(_chat_text('{"tool": "done", "args": {"summary": "s"}}'))
    assert driver.next_action(SYSTEM, TRANSCRIPT).tool == "done"
    assert client.calls[2]["response_format"] == {"type": "json_object"}


def test_openai_retry_once_on_status_error():
    import openai

    err = openai.APIStatusError("overloaded", response=SimpleNamespace(status_code=503, headers={}, request=None), body=None)
    client = FakeOpenAIClient([err, _chat_tool_call({"tool": "done", "args": {"summary": "fine"}})])
    assert OpenAICompatDriver("m", provider="openai", client=client).next_action(SYSTEM, TRANSCRIPT).tool == "done"
    client = FakeOpenAIClient([err, err])
    with pytest.raises(RuntimeError, match="after retry"):
        OpenAICompatDriver("m", provider="openai", client=client).next_action(SYSTEM, TRANSCRIPT)


def test_provider_settings_from_env(monkeypatch):
    monkeypatch.delenv("LILAC_API_KEY", raising=False)
    monkeypatch.delenv("LILAC_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="LILAC_API_KEY"):
        provider_settings("lilac")
    monkeypatch.setenv("LILAC_API_KEY", "lk")
    assert provider_settings("lilac") == (LILAC_DEFAULT_BASE_URL, "lk")
    monkeypatch.setenv("LILAC_BASE_URL", "https://example.test/v1")
    assert provider_settings("lilac") == ("https://example.test/v1", "lk")
    monkeypatch.setenv("OPENAI_API_KEY", "ok")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert provider_settings("openai") == ("https://api.openai.com/v1", "ok")
    with pytest.raises(ValueError):
        OpenAICompatDriver("m", provider="azure")


def test_create_driver_routes_providers(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "C:/fake/claude.exe")
    assert isinstance(create_driver("claude-cli:haiku"), ClaudeCLIDriver)
    assert create_driver("claude-cli:haiku").model_id == "claude-cli:haiku"
    assert isinstance(create_driver("anthropic:claude-sonnet-5"), AnthropicDriver)
    assert isinstance(create_driver("lilac:openai/gpt-oss-120b"), OpenAICompatDriver)
    assert create_driver("openai:gpt-4.1-mini").provider == "openai"


# --- claude cli -------------------------------------------------------------------------

def test_render_transcript_shape():
    text = render_transcript(TRANSCRIPT)
    assert text.startswith("### USER\n## Issue\nadd() subtracts\n\n### ASSISTANT\n")
    assert "\n### USER\nTOOL RESULT read_file:\n" in text
    assert text.endswith(TAIL_PROMPT)


def test_cli_schema_is_flat():
    # the CLI gets every argument at the top level: models handed the nested shape kept wrapping the whole action in "args"
    schema = cli_schema()
    props = schema["properties"]
    assert schema["type"] == "object" and schema["required"] == ["tool"]
    assert props["tool"]["enum"] == TOOL_NAMES and props["thought"]["type"] == "string"
    assert set(props) == {"thought", "tool", "path", "content", "old", "new", "summary", "start", "end"}
    assert "args" not in props and not any(p.get("type") == "object" for p in props.values())
    for key in ("path", "content", "old", "new", "summary"):
        assert props[key]["type"] == "string" and props[key]["description"]
    for key in ("start", "end"):
        assert props[key]["type"] == "integer" and props[key]["description"]
    # the nested schema the API drivers use knows the same argument keys
    assert set(ACTION_SCHEMA["properties"]["args"]["properties"]) == set(props) - {"thought", "tool"}
    assert "description" not in ACTION_SCHEMA["properties"]["args"]["properties"]["path"]
    assert "FLAT" in SYSTEM_SUFFIX and '"old": "..."' in SYSTEM_SUFFIX and 'inside an "args"' in SYSTEM_SUFFIX
    assert "no \"args\" wrapper" in TAIL_PROMPT


def test_render_transcript_shows_assistant_turns_flat():
    turns = [
        {"role": "user", "content": "## Issue\nx"},
        {"role": "assistant", "content": '{"thought": "look", "tool": "read_file", "args": {"path": "a.py", "start": 3, "end": 9}}'},
        {"role": "user", "content": "TOOL RESULT read_file:\n3: x"},
        {"role": "assistant", "content": "not json at all"},
    ]
    text = render_transcript(turns)
    assert '### ASSISTANT\n{"thought": "look", "tool": "read_file", "path": "a.py", "start": 3, "end": 9}\n' in text
    assert '"args"' not in text.split(TAIL_PROMPT)[0]
    assert "### ASSISTANT\nnot json at all\n" in text
    assert flatten_action_json('{"thought": "t", "tool": "write_file", "args": {"path": "a.py", "old": "x", "new": "y"}}') == '{"thought": "t", "tool": "write_file", "path": "a.py", "old": "x", "new": "y"}'
    assert flatten_action_json('{"tool": "nope", "args": {}}') == '{"tool": "nope", "args": {}}'


def test_claude_cli_flat_structured_output_becomes_action():
    cases = [
        ({"thought": "t", "tool": "read_file", "path": "src/marshmallow/validate.py"}, "read_file", {"path": "src/marshmallow/validate.py"}, "t"),
        ({"thought": "t", "tool": "read_file", "path": "a.py", "start": 10, "end": 40}, "read_file", {"path": "a.py", "start": 10, "end": 40}, "t"),
        ({"thought": "t", "tool": "write_file", "path": "a.py", "old": "x = 1", "new": "x = 2"}, "write_file", {"path": "a.py", "old": "x = 1", "new": "x = 2"}, "t"),
        ({"thought": "t", "tool": "write_file", "path": "a.py", "content": "x = 2\n", "old": "", "summary": None}, "write_file", {"path": "a.py", "content": "x = 2\n"}, "t"),
        ({"thought": "t", "tool": "run_tests"}, "run_tests", {}, "t"),
        ({"tool": "done", "summary": "fixed"}, "done", {"summary": "fixed"}, ""),
        # the nested shapes the model produced under the old schema still yield full arguments
        ({"args": {"tool": "read_file", "args": {"path": "src/marshmallow/validate.py"}}, "thought": "t"}, "read_file", {"path": "src/marshmallow/validate.py"}, "t"),
        ({"args": {"tool": "read_file", "path": "src/marshmallow/validate.py"}, "thought": "t"}, "read_file", {"path": "src/marshmallow/validate.py"}, "t"),
        ({"action": {"tool": "write_file", "input": {"path": "a.py", "old": "x", "new": "y"}}}, "write_file", {"path": "a.py", "old": "x", "new": "y"}, ""),
        ({"tool": "write_file", "path": "a.py", "args": {"old": "x", "new": "y"}}, "write_file", {"path": "a.py", "old": "x", "new": "y"}, ""),
        ({"tool": "read_file", "args": {"file": "a.py"}}, "read_file", {"file": "a.py"}, ""),  # aliases survive for normalize_args
        ({"thought": "outer", "args": {"thought": "inner", "tool": "list_files", "path": "src"}}, "list_files", {"path": "src"}, "inner"),
    ]
    for so, want_tool, want_args, want_thought in cases:
        result = {"type": "result", "is_error": False, "structured_output": so, "result": ""}
        action = action_from_stream(result, [])
        assert action is not None, so
        assert (action.tool, action.args, action.thought) == (want_tool, want_args, want_thought), so
        assert json.loads(action.raw) == so
    assert action_from_stream({"type": "result", "structured_output": {"thought": "no tool"}}, []) is None
    # the same shapes recovered from plain text when the model never called StructuredOutput
    nested_text = 'Sure.\n{"args": {"tool": "read_file", "args": {"path": "a.py"}}, "thought": "t"}\n### USER\n...'
    blocks = [{"type": "text", "text": nested_text}]
    action = action_from_stream({"type": "result", "is_error": True, "subtype": "error_max_turns"}, blocks)
    assert action.tool == "read_file" and action.args == {"path": "a.py"} and action.thought == "t"
    assert parse_action('{"thought": "t", "tool": "write_file", "path": "a.py", "old": "x", "new": "y"}').args == {"path": "a.py", "old": "x", "new": "y"}


def _fake_cli_result(payload: dict[str, Any], is_error: bool = False, rc: int = 0) -> subprocess.CompletedProcess:
    lines = [json.dumps({"type": "system", "subtype": "init"}), json.dumps({"type": "assistant", "message": {}}), json.dumps({"type": "result", "is_error": is_error, **payload})]
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="\n".join(lines).encode("utf-8"), stderr=b"")


def test_claude_cli_argv_stdin_env_and_parsing(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    calls: list[dict[str, Any]] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return _fake_cli_result({"structured_output": {"thought": "read it", "tool": "read_file", "args": {"path": "pkg/calc.py"}}, "result": "{}"})

    monkeypatch.setattr(subprocess, "run", fake_run)
    driver = ClaudeCLIDriver("haiku", exe="C:/fake/claude.exe", timeout=77)
    action = driver.next_action(SYSTEM, TRANSCRIPT)

    call = calls[0]
    argv = call["argv"]
    assert argv[:9] == ["C:/fake/claude.exe", "-p", "--model", "haiku", "--no-session-persistence", "--tools", "", "--system-prompt", SYSTEM + SYSTEM_SUFFIX]
    assert argv[9:] == ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--max-turns", "1", "--json-schema", json.dumps(cli_schema())]
    assert isinstance(argv, list) and "shell" not in call
    stdin = call["input"].decode("utf-8")
    assert stdin.endswith("\n") and stdin.count("\n") == 1
    msg = json.loads(stdin)
    assert msg["type"] == "user" and msg["message"]["role"] == "user"
    assert msg["message"]["content"] == render_transcript(TRANSCRIPT)
    assert "CLAUDECODE" not in call["env"] and call["env"]["PYTHONIOENCODING"] == "utf-8"
    assert call["timeout"] == 77 and call["capture_output"] is True
    assert action.tool == "read_file" and action.args == {"path": "pkg/calc.py"} and action.thought == "read it"


def test_claude_cli_falls_back_to_result_text_and_retries(monkeypatch):
    responses = [
        _fake_cli_result({"result": "rate limited", "structured_output": None}, is_error=True, rc=1),
        _fake_cli_result({"result": 'Sure: {"tool": "run_tests", "args": {}}'}),
    ]
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: responses.pop(0))
    driver = ClaudeCLIDriver("sonnet", exe="C:/fake/claude.exe")
    assert driver.next_action(SYSTEM, TRANSCRIPT).tool == "run_tests"
    assert responses == []

    errors = [subprocess.TimeoutExpired(cmd="claude", timeout=1), subprocess.TimeoutExpired(cmd="claude", timeout=1)]

    def timeout_run(argv, **kw):
        raise errors.pop(0)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    with pytest.raises(RuntimeError, match="timed out"):
        driver.next_action(SYSTEM, TRANSCRIPT)


def test_claude_cli_recovers_plain_text_json_from_max_turns_error():
    # haiku wrote the JSON as text (and kept role-playing the transcript) instead of calling StructuredOutput
    text = ('{"thought": "check the impl", "tool": "read_file", "args": {"path": "inventory/cart.py"}}\n\n'
            '### USER\nTOOL RESULT read_file:\n1: class Cart: ...\n{"thought": "done", "tool": "done", "args": {"summary": "x"}}')
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}),
        json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns", "errors": ["Reached maximum number of turns (2)"]}),
    ]
    result, blocks = parse_stream("\n".join(lines))
    assert result["subtype"] == "error_max_turns" and len(blocks) == 2
    action = action_from_stream(result, blocks)
    assert action.tool == "read_file" and action.args == {"path": "inventory/cart.py"} and action.thought == "check the impl"
    assert first_action_object('garbage {not json} {"tool": "nope"} {"tool": "run_tests", "args": {}} tail') == {"tool": "run_tests", "args": {}}
    assert first_action_object("nothing here") is None


def test_claude_cli_recovers_native_tool_use_block():
    # haiku emitted a real tool_use block named after one of our tools; the CLI has no such tool but we do
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "write_file", "input": {"path": "a.py", "content": "x = 1\n"}}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "No such tool available: write_file"}]}}),
        json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns", "errors": ["Reached maximum number of turns (2)"]}),
    ]
    action = action_from_stream(*parse_stream("\n".join(lines)))
    assert action.tool == "write_file" and action.args == {"path": "a.py", "content": "x = 1\n"}
    # the StructuredOutput tool_use block is honoured too when structured_output is missing from the result
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "StructuredOutput", "input": {"tool": "run_tests", "args": {}}}]}}),
        json.dumps({"type": "result", "is_error": False, "result": ""}),
    ]
    assert action_from_stream(*parse_stream("\n".join(lines))).tool == "run_tests"
    # sonnet nested the action under "args" and failed the CLI schema check; lift it out of the StructuredOutput call
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "StructuredOutput", "input": {"args": {"tool": "list_files", "args": {"path": "inventory"}}, "thought": "find the Cart class"}}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "Output does not match required schema: root: must have required property 'tool'"}]}}),
        json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns", "errors": ["Reached maximum number of turns (1)"]}),
    ]
    action = action_from_stream(*parse_stream("\n".join(lines)))
    assert action.tool == "list_files" and action.args == {"path": "inventory"} and action.thought == "find the Cart class"
    # a genuine failure with nothing to recover yields no action (the driver raises with the CLI's error text)
    lines = [json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns", "errors": ["Reached maximum number of turns (2)"]})]
    assert action_from_stream(*parse_stream("\n".join(lines))) is None


def test_claude_cli_error_message_carries_cli_errors(monkeypatch):
    stream = json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns", "errors": ["Reached maximum number of turns (2)"]})
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(args=[], returncode=1, stdout=stream.encode(), stderr=b""))
    with pytest.raises(RuntimeError, match="Reached maximum number of turns"):
        ClaudeCLIDriver("haiku", exe="C:/fake/claude.exe").next_action(SYSTEM, TRANSCRIPT)


def test_claude_cli_system_prompt_is_truncated_on_argv():
    driver = ClaudeCLIDriver("haiku", exe="C:/fake/claude.exe")
    argv = driver.build_argv("x" * 30_000)
    assert len(argv[argv.index("--system-prompt") + 1]) == 20_000


@pytest.mark.skipif(not shutil.which("claude"), reason="claude CLI not installed")
def test_claude_cli_real_call():
    driver = create_driver("claude-cli:haiku")
    system = "You are a coding agent. Tools: list_files{path}, read_file{path}, write_file{path,content}, run_tests{path?}, done{summary}. Reply with one JSON action."
    action = driver.next_action(system, [{"role": "user", "content": "## Issue\nadd() returns a - b instead of a + b.\n\n## Failing tests\ntests/test_calc.py::test_add\n\n## Repository (top level)\npkg/\ntests/\nREADME.md"}])
    print("\nreal CLI action:", action.tool, action.args, "|", action.thought)
    assert action.tool in TOOL_NAMES
    assert action.tool != "done"
    assert action.raw
