"""Env-driven settings. Everything has a default that works with no keys on a bare laptop."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, never overrides a set variable."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(ROOT / ".env")
WORK_DIR = Path(os.environ.get("REWIND_WORK_DIR", ROOT / ".rewind"))
DB_PATH = Path(os.environ.get("REWIND_DB", ROOT / ".rewind" / "runs.sqlite"))

MAX_INSTANCES = int(os.environ.get("REWIND_MAX_INSTANCES", "5"))
MAX_COMMITS_SCANNED = int(os.environ.get("REWIND_MAX_COMMITS", "400"))
STEP_CAP = int(os.environ.get("REWIND_STEP_CAP", "15"))
TEST_TIMEOUT = int(os.environ.get("REWIND_TEST_TIMEOUT", "120"))
LLM_TIMEOUT = int(os.environ.get("REWIND_LLM_TIMEOUT", "300"))  # a whole-file write of a 25k-char file takes > 120s through the CLI

# Sandbox backend: "auto" picks daytona if DAYTONA_API_KEY is set, else docker if docker is on PATH, else local.
SANDBOX_BACKEND = os.environ.get("REWIND_SANDBOX", "auto")

# Comma-separated model config strings. Forms:
#   claude-cli:<haiku|sonnet|opus>          -> drives the local `claude` CLI (no API key needed)
#   anthropic:<model-id>                    -> Anthropic Messages API (ANTHROPIC_API_KEY)
#   openai:<model-id>                       -> any OpenAI-compatible endpoint (OPENAI_BASE_URL, OPENAI_API_KEY)
#   lilac:<model-id>                        -> OpenAI-compatible, uses LILAC_BASE_URL + LILAC_API_KEY
def default_models() -> list[str]:
    env = os.environ.get("REWIND_MODELS")
    if env:
        return [m.strip() for m in env.split(",") if m.strip()]
    models: list[str] = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        models.append("anthropic:claude-sonnet-5")
    else:
        models.append("claude-cli:sonnet")
    if os.environ.get("LILAC_API_KEY"):
        models.append("lilac:" + os.environ.get("LILAC_MODEL", "openai/gpt-oss-120b"))
    elif os.environ.get("OPENAI_API_KEY"):
        models.append("openai:" + os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))
    else:
        models.append("claude-cli:haiku")
    return models


MODEL_LABELS = {
    "claude-cli:haiku": "Claude Haiku 4.5",
    "claude-cli:sonnet": "Claude Sonnet",
    "claude-cli:opus": "Claude Opus",
    "anthropic:claude-sonnet-5": "Claude Sonnet 5",
    "anthropic:claude-opus-5": "Claude Opus 5",
    "anthropic:claude-haiku-4-5-20251001": "Claude Haiku 4.5",
}


def model_label(model_id: str) -> str:
    if model_id in MODEL_LABELS:
        return MODEL_LABELS[model_id]
    provider, _, name = model_id.partition(":")
    return name or provider
