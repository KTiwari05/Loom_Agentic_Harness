"""Tool registry + a decorator that builds JSON schemas from Python type hints.

Write tools as plain typed functions with a docstring; the schema the model sees is
generated automatically. Don't hand-write JSON schemas. See docs/04-tech-stack.md.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass
from typing import Any, Callable

REGISTRY: dict[str, "ToolMeta"] = {}

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class ToolMeta:
    name: str
    fn: Callable[..., str]
    schema: dict[str, Any]
    risky: bool


def tool(*, risky: bool = False) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a function as a tool. `risky=True` routes it through the permission gate."""

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        schema = _build_schema(fn)
        REGISTRY[fn.__name__] = ToolMeta(name=fn.__name__, fn=fn, schema=schema, risky=risky)
        return fn

    return decorator


def _build_schema(fn: Callable[..., str]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    description, param_docs = _parse_doc(inspect.getdoc(fn) or "")

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        annotation = hints.get(name, str)
        optional, base = _unwrap_optional(annotation)
        prop: dict[str, Any] = {"type": _json_type(base)}
        if name in param_docs:
            prop["description"] = param_docs[name]
        properties[name] = prop
        if param.default is inspect.Parameter.empty and not optional:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _json_type(base: Any) -> str:
    if base in _TYPE_MAP:
        return _TYPE_MAP[base]
    origin = typing.get_origin(base)
    if origin in (list, tuple, set):
        return "array"
    if origin is dict:
        return "object"
    return "string"


def _unwrap_optional(annotation: Any) -> tuple[bool, Any]:
    """Optional[X] / X | None -> (True, X). Otherwise (False, annotation)."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return True, (args[0] if args else str)
    return False, annotation


def _parse_doc(doc: str) -> tuple[str, dict[str, str]]:
    """Split a Google-style docstring into (description, {param: description})."""
    lines = doc.splitlines()
    desc_lines: list[str] = []
    param_docs: dict[str, str] = {}
    in_args = False
    for raw in lines:
        line = raw.strip()
        if line.lower() in {"args:", "arguments:", "params:", "parameters:"}:
            in_args = True
            continue
        if in_args:
            if not line or line.endswith(":"):  # blank line or a new section header
                in_args = False
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                param_docs[key.strip()] = val.strip()
        else:
            desc_lines.append(raw)
    return "\n".join(desc_lines).strip(), param_docs
