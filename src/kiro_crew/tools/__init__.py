"""Tool registry. A tool *factory* takes the AppContext and returns Strands tool(s).

Agents reference tools by name in YAML:
  - ``time``                      a registered factory (see ``builtin.py``)
  - ``my_pkg.my_tool``            an importable module with TOOL_SPEC (classic Strands module tool)
  - ``my_pkg.tools:search``       ``module:attr`` for a specific tool object, e.g. ``strands.vended_tools:sleep``
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from ..core.context import AppContext
from ..core.registry import Registry

ToolFactory = Callable[[AppContext], Any]  # returns a tool, a module, or a list of them
tool_factories: Registry[ToolFactory] = Registry("tool")


def register_builtin_tools() -> None:
    """Import every built-in tool module so its factories register (idempotent)."""
    from ..scheduler import tools as _schedule  # noqa: F401
    from ..tasks import tools as _tasks  # noqa: F401
    from . import builtin, sandbox_tools, web  # noqa: F401


def resolve_tools(specs: list[str], ctx: AppContext) -> list[Any]:
    register_builtin_tools()

    out: list[Any] = []
    for spec in specs:
        if spec in tool_factories:
            made = tool_factories.get(spec)(ctx)
        elif ":" in spec:
            mod, attr = spec.split(":", 1)
            made = getattr(importlib.import_module(mod), attr)
        else:
            made = importlib.import_module(spec)  # strands module-style tool
        out.extend(made if isinstance(made, list) else [made])
    return out
