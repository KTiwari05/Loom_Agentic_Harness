"""Importing this package registers every built-in tool into the REGISTRY."""

from . import fs, search, shell  # noqa: F401  (side effect: registration)
from .base import REGISTRY, ToolMeta, tool

__all__ = ["REGISTRY", "ToolMeta", "tool"]
