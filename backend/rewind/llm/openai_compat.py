"""OpenAI-compatible chat.completions driver: provider "openai" (OPENAI_BASE_URL/OPENAI_API_KEY) or "lilac".

Lilac (getlilac.com) exposes an OpenAI-compatible endpoint; its documented base URL is https://api.getlilac.com/v1
(docs.getlilac.com/inference/quickstart). LILAC_BASE_URL overrides it; LILAC_API_KEY is required.

The action is forced through a `act` function tool. Endpoints that reject `tool_choice` (HTTP 400) get a
second try with `response_format: json_object` and lenient parsing; that mode then sticks for the driver.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from ..config import LLM_TIMEOUT
from . import ACTION_SCHEMA, Action, LLMDriver, parse_action
from .anthropic_api import action_from_input, merge_turns

LILAC_DEFAULT_BASE_URL = "https://api.getlilac.com/v1"
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
ACT_FUNCTION = {
    "type": "function",
    "function": {
        "name": "act",
        "description": "Choose the next tool call. Exactly one action per turn.",
        "parameters": ACTION_SCHEMA,
    },
}
JSON_MODE_HINT = "\n\nRespond with a single JSON object: {\"thought\": str, \"tool\": str, \"args\": object}."


def provider_settings(provider: str) -> tuple[str, str]:
    """(base_url, api_key) for a provider, read from the environment."""
    if provider == "lilac":
        key = os.environ.get("LILAC_API_KEY", "")
        if not key:
            raise RuntimeError("LILAC_API_KEY is not set")
        return os.environ.get("LILAC_BASE_URL", LILAC_DEFAULT_BASE_URL), key
    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        return os.environ.get("OPENAI_BASE_URL", OPENAI_DEFAULT_BASE_URL), key
    raise ValueError(f"unknown OpenAI-compatible provider {provider!r}")


class OpenAICompatDriver(LLMDriver):
    def __init__(self, model_name: str, provider: str = "openai", client: Any = None, timeout: int = LLM_TIMEOUT):
        if provider not in ("openai", "lilac"):
            raise ValueError(f"unknown OpenAI-compatible provider {provider!r}")
        if not model_name:
            raise ValueError(f"{provider}: a model id is required, e.g. {provider}:<model>")
        self.model_name = model_name
        self.provider = provider
        self.model_id = f"{provider}:{model_name}"
        self.timeout = timeout
        self._client = client
        self.json_mode = False

    @property
    def client(self) -> Any:
        if self._client is None:
            import openai

            base_url, api_key = provider_settings(self.provider)
            self._client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=float(self.timeout), max_retries=1)
        return self._client

    def build_request(self, system: str, transcript: list[dict[str, str]]) -> dict[str, Any]:
        sys_text = system + (JSON_MODE_HINT if self.json_mode else "")
        req: dict[str, Any] = {
            "model": self.model_name,
            "messages": [{"role": "system", "content": sys_text}] + merge_turns(transcript),
        }
        if self.json_mode:
            req["response_format"] = {"type": "json_object"}
        else:
            req["tools"] = [ACT_FUNCTION]
            req["tool_choice"] = {"type": "function", "function": {"name": "act"}}
        return req

    @staticmethod
    def parse_response(response: Any) -> Action:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return parse_action("")
        message = choices[0].message
        for call in getattr(message, "tool_calls", None) or []:
            fn = getattr(call, "function", None)
            if fn is None or getattr(fn, "name", "") != "act":
                continue
            raw = getattr(fn, "arguments", "") or "{}"
            try:
                inp = json.loads(raw)
            except ValueError:
                return parse_action(raw)
            return action_from_input(inp, raw)
        return parse_action(getattr(message, "content", "") or "")

    def next_action(self, system: str, transcript: list[dict[str, str]]) -> Action:
        import openai

        last: Optional[Exception] = None
        for _ in range(2):
            try:
                return self.parse_response(self.client.chat.completions.create(**self.build_request(system, transcript)))
            except openai.BadRequestError as e:
                if not self.json_mode:
                    self.json_mode = True  # endpoint rejects tool_choice/tools; switch to JSON mode for good
                    last = e
                    continue
                raise
            except (openai.APIStatusError, openai.APITimeoutError, openai.APIConnectionError) as e:
                last = e
        raise RuntimeError(f"{self.provider} request failed after retry: {last}")
