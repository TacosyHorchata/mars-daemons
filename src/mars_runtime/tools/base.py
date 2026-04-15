"""Tool protocol types — Tool, ToolOutput.

Kept separate from the registry so new providers (third-party tools,
dynamic plugins) can import just the types without triggering
registry side-effects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[[dict[str, Any]], ToolOutput]
