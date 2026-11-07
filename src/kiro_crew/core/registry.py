"""Generic plugin registry.

Every extension point (providers, channels, tools, memory backends) is a ``Registry``.
Implementations are added in one of three ways, so the core never needs editing:

1. decorator:      ``@providers.register("my-llm")``
2. entry points:   ``[project.entry-points."kiro_crew.providers"]`` in a third-party package
3. config modules: ``plugins: [my_pkg.my_module]`` in the YAML (module import triggers #1)
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, T] = {}
        self._entry_points_loaded = False

    def register(self, name: str) -> Callable[[T], T]:
        def deco(obj: T) -> T:
            self.add(name, obj)
            return obj

        return deco

    def add(self, name: str, obj: T) -> None:
        if name in self._items and self._items[name] is not obj:
            logger.warning("%s %r is being overridden", self.kind, name)
        self._items[name] = obj

    def get(self, name: str) -> T:
        self._load_entry_points()
        try:
            return self._items[name]
        except KeyError:
            raise KeyError(f"Unknown {self.kind} {name!r}. Available: {sorted(self._items)}") from None

    def names(self) -> list[str]:
        self._load_entry_points()
        return sorted(self._items)

    def __contains__(self, name: str) -> bool:
        self._load_entry_points()
        return name in self._items

    def _load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        self._entry_points_loaded = True
        for ep in entry_points(group=f"kiro_crew.{self.kind}s"):
            try:
                self._items.setdefault(ep.name, ep.load())
            except Exception:  # a broken third-party plugin must not break the app
                logger.exception("Failed to load %s plugin %r", self.kind, ep.name)


def import_plugin_modules(modules: list[str]) -> None:
    """Import user modules listed under ``plugins:`` so their decorators run."""
    for mod in modules:
        importlib.import_module(mod)
        logger.info("Loaded plugin module %s", mod)
