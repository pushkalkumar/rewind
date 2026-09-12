"""Anthropic Messages API driver. The action is forced through a single tool named `act` whose input is ACTION_SCHEMA."""
from __future__ import annotations

import json
from typing import Any, Optional

from ..config import LLM_TIMEOUT
from . import ACTION_SCHEMA, TOOL_NAMES, Action, LLMDriver, parse_action

DEFAULT_MODEL = "claude-sonnet-5"
ACT_TOOL = {
    "name": "act",
    "description": "Choose the next tool call. Exactly one action per turn.",
    "input_schema": ACTION_SCHEMA,
}
MAX_TOKENS = 8192


def merge_turns(transcript: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse consecutive same-role turns and guarantee the conversation starts with a user turn."""
    merged: list[dict[str, str]] = []
    for turn in transcript:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        content = str(turn.get("content", ""))
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + content
        else:
            merged.append({"role": role, "content": content})
    if not merged or merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "Begin."})
    return merged


def action_from_input(inp: dict[str, Any], raw_text: str = "") -> Action:
    if isinstance(inp, dict) and inp.get("tool") in TOOL_NAMES:
        args = inp.get("args") or {}
        return Action(tool=inp["tool"], args=args if isinstance(args, dict) else {}, thought=str(inp.get("thought", "")), raw=json.dumps(inp))
    return parse_action(raw_text or json.dumps(inp))


class AnthropicDriver(LLMDriver):
    def __init__(self, model_name: str = "", client: Any = None, timeout: int = LLM_TIMEOUT):
        self.model_name = model_name or DEFAULT_MODEL
        self.model_id = f"anthropic:{self.model_name}"
        self.timeout = timeout
        self._client = client
        self.force_tool = True

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(timeout=float(self.timeout), max_retries=1)
        return self._client

    def build_request(self, system: str, transcript: list[dict[str, str]]) -> dict[str, Any]:
        req: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": merge_turns(transcript),
            "tools": [ACT_TOOL],
        }
        if self.force_tool:
            req["tool_choice"] = {"type": "tool", "name": "act"}
        else:
            req["tool_choice"] = {"type": "auto"}
        return req

    @staticmethod
    def parse_response(response: Any) -> Action:
        text = ""
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "tool_use" and getattr(block, "name", "") == "act":
                return action_from_input(getattr(block, "input", {}) or {})
            if btype == "text":
                text += getattr(block, "text", "") or ""
        return parse_action(text)

    def next_action(self, system: str, transcript: list[dict[str, str]]) -> Action:
        import anthropic

        last: Optional[Exception] = None
        for _ in range(2):
            try:
                return self.parse_response(self.client.messages.create(**self.build_request(system, transcript)))
            except anthropic.BadRequestError as e:
                if self.force_tool and "tool_choice" in str(e):
                    self.force_tool = False  # model rejects forced tool use; fall back to auto + schema parse
                    last = e
                    continue
                raise
            except (anthropic.APIStatusError, anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
                last = e
        raise RuntimeError(f"anthropic request failed after retry: {last}")
