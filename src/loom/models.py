"""The model layer: one call into any provider via LiteLLM, plus cost accounting.

Phase 1 keeps this deliberately small. The Conductor (Phase 3) will choose *which*
model to pass here; this module only knows how to *call* a model and price the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import litellm

# Quieter, more forgiving defaults for open models behind varied providers.
litellm.suppress_debug_info = True  # no "Give Feedback / Provider List" banner on errors
litellm.drop_params = True  # silently drop params a given provider doesn't support

# Fallback pricing in USD per 1M tokens (input, output). Used only if LiteLLM can't
# price a model itself. Illustrative mid-2026 numbers — see docs/05-models-and-cost-routing.md.
PRICING: dict[str, tuple[float, float]] = {
    "moonshotai/kimi-k2": (0.60, 2.50),
    "moonshotai/kimi-k2.5": (0.60, 2.50),
    "moonshotai/kimi-k2.6": (0.95, 4.00),
    "z-ai/glm-5.1": (0.60, 2.20),
    "deepseek/deepseek-v4": (0.40, 1.60),
}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string from the model


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall]
    assistant_message: dict[str, Any]  # ready to append to the message history
    usage: dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    model: str = ""  # the ACTUAL model that answered (may differ from the one requested)
    request_id: str = ""  # provider request id; "" → the event log mints a local one


def complete(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float = 0.2,
) -> ModelResponse:
    """One synchronous turn. Returns text + any tool calls the model wants to make."""
    resp = litellm.completion(
        model=model,
        messages=messages,
        tools=tools or None,
        tool_choice="auto" if tools else None,
        temperature=temperature,
    )
    msg = resp.choices[0].message
    raw_tool_calls = getattr(msg, "tool_calls", None) or []

    tool_calls = [
        ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
        for tc in raw_tool_calls
    ]

    assistant_message: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if raw_tool_calls:
        assistant_message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            }
            for tc in raw_tool_calls
        ]

    usage_obj = getattr(resp, "usage", None)
    usage = {
        "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
    }
    # Cache metrics when the provider reports them (OpenAI-style details or
    # Anthropic-style fields on usage); absent → the record keeps them null.
    details = getattr(usage_obj, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    if cached is None:
        cached = getattr(usage_obj, "cache_read_input_tokens", None)
    if cached is not None:
        usage["cache_read_input_tokens"] = int(cached)
    created = getattr(usage_obj, "cache_creation_input_tokens", None)
    if created is not None:
        usage["cache_creation_input_tokens"] = int(created)

    return ModelResponse(
        text=msg.content or "",
        tool_calls=tool_calls,
        assistant_message=assistant_message,
        usage=usage,
        cost=_estimate_cost(resp, model, usage),
        model=str(getattr(resp, "model", "") or model),
        request_id=str(getattr(resp, "id", "") or ""),
    )


def _estimate_cost(resp: Any, model: str, usage: dict[str, int]) -> float:
    """Prefer LiteLLM's own pricing; fall back to the table above."""
    try:
        cost = litellm.completion_cost(completion_response=resp)
        if cost:
            return float(cost)
    except Exception:
        pass

    # Fallback: match the bare model name against the pricing table.
    key = model.split("/", 1)[-1] if "/" in model else model
    rates = PRICING.get(key) or PRICING.get(model)
    if not rates:
        return 0.0
    in_rate, out_rate = rates
    return (usage["prompt_tokens"] * in_rate + usage["completion_tokens"] * out_rate) / 1_000_000
