"""LLM driver interface. A driver turns a transcript into the next tool call; the agent loop owns everything else.

Transcript is provider-agnostic: a list of {"role": "user"|"assistant", "content": str}. Assistant turns are
the JSON action the model chose; user turns are tool results. Every driver must return an Action.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

TOOL_NAMES = ["list_files", "read_file", "write_file", "run_tests", "done"]

# Every argument any tool accepts. Drivers that cannot nest (the claude CLI) put these at the top level.
ARG_PROPERTIES: dict[str, dict[str, Any]] = {
    "path": {"type": "string"},
    "content": {"type": "string"},
    "old": {"type": "string"},
    "new": {"type": "string"},
    "summary": {"type": "string"},
    "start": {"type": "integer"},
    "end": {"type": "integer"},
}

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string", "description": "One or two sentences of reasoning."},
        "tool": {"type": "string", "enum": TOOL_NAMES},
        "args": {
            "type": "object",
            "properties": {k: dict(v) for k, v in ARG_PROPERTIES.items()},
            "additionalProperties": True,
        },
    },
    "required": ["tool", "args"],
}


@dataclass
class Action:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""
    raw: str = ""


class LLMDriver(ABC):
    model_id: str = ""

    @abstractmethod
    def next_action(self, system: str, transcript: list[dict[str, str]]) -> Action: ...


def parse_action(text: str) -> Action:
    """Lenient JSON extraction for drivers that can't force a schema."""
    import json
    import re

    text = text.strip()
    candidates = [text]
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        candidates.insert(0, m.group(1))
    start = text.find("{")
    if start != -1:
        candidates.append(text[start:])
    for c in candidates:
        try:
            obj = json.loads(c)
        except Exception:
            # try trimming trailing junk
            end = c.rfind("}")
            if end == -1:
                continue
            try:
                obj = json.loads(c[: end + 1])
            except Exception:
                continue
        if isinstance(obj, dict) and obj.get("tool") in TOOL_NAMES:
            return Action(tool=obj["tool"], args=action_args(obj), thought=str(obj.get("thought", "")), raw=text)
    return Action(tool="done", args={"summary": "(unparseable response) " + text[:300]}, thought="", raw=text)


def action_args(obj: dict[str, Any]) -> dict[str, Any]:
    """The arguments of an action object, whether they sit under "args" or flat at the top level next to "tool"."""
    args = obj.get("args")
    out: dict[str, Any] = {k: v for k, v in args.items() if v not in (None, "")} if isinstance(args, dict) else {}
    for k, v in obj.items():
        if k in ("tool", "thought", "args") or isinstance(v, (dict, list)) or v in (None, ""):
            continue
        out.setdefault(k, v)
    return out


def create_driver(model_id: str) -> LLMDriver:
    provider, _, name = model_id.partition(":")
    if provider == "claude-cli":
        from .claude_cli import ClaudeCLIDriver
        return ClaudeCLIDriver(name or "sonnet")
    if provider == "anthropic":
        from .anthropic_api import AnthropicDriver
        return AnthropicDriver(name)
    if provider in ("openai", "lilac"):
        from .openai_compat import OpenAICompatDriver
        return OpenAICompatDriver(name, provider=provider)
    raise ValueError(f"unknown model provider in {model_id!r}")
