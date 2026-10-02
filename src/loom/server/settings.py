"""User settings for the desktop app — bring-your-own-key, any provider.

Stored in ~/.loom/loom.env (simple KEY=value lines). Loaded into os.environ on server
start, so a fresh Config() and LiteLLM both pick the values up immediately. The web UI
never sees full API keys back — only a masked tail (see `masked`).
"""

from __future__ import annotations

import os
from pathlib import Path

LOOM_HOME = Path.home() / ".loom"
ENV_FILE = LOOM_HOME / "loom.env"

# Provider keys the settings screen offers. LiteLLM routes by model prefix, so setting
# the right key is all it takes: anthropic/..., openai/..., openrouter/..., deepseek/...
PROVIDER_KEYS = [
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "XAI_API_KEY",
]

# Non-secret knobs the settings screen edits.
PLAIN_FIELDS = [
    "LOOM_DISPLAY_NAME",
    "LOOM_MODEL",
    "LOOM_MODEL_STRONG",
    "LOOM_MODEL_CRITIC",
    "LOOM_CRITIC",
    "LOOM_MAX_STEPS",
    "LOOM_MAX_COST_USD",
]

ALL_FIELDS = PROVIDER_KEYS + PLAIN_FIELDS


def load_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.is_file():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def apply_to_env() -> None:
    """Load saved settings into the process env. Explicit env vars win over the file."""
    for key, value in load_file().items():
        os.environ.setdefault(key, value)


def save(updates: dict[str, str]) -> None:
    """Merge updates into loom.env. Empty string deletes a key. Unknown keys are ignored."""
    values = load_file()
    for key, value in updates.items():
        if key not in ALL_FIELDS:
            continue
        value = str(value).strip()
        if value:
            values[key] = value
            os.environ[key] = value
        else:
            values.pop(key, None)
            os.environ.pop(key, None)
    LOOM_HOME.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in sorted(values.items())]
    ENV_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def masked() -> dict[str, str]:
    """Settings for the UI: secrets reduced to a masked tail, plain fields as-is."""
    out: dict[str, str] = {}
    for key in ALL_FIELDS:
        value = os.environ.get(key, "")
        if key in PROVIDER_KEYS and value:
            out[key] = "•••• " + value[-4:]
        else:
            out[key] = value
    return out
