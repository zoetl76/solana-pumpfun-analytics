"""Registre des stratégies : ``get_strategy(name, params)`` et ``list_strategies()``.

Le nom d'une stratégie est le nom de son module dans ``mt5/strategies``
(ex. ``ema_cross`` → ``mt5/strategies/ema_cross.py``). Le module doit exposer
soit une variable ``STRATEGY`` (la classe), soit exactement une sous-classe de
``Strategy`` définie dans le module.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any

from mt5.engine.strategy import Strategy

_EXCLUDED = {"registry", "__init__"}


def list_strategies() -> list[str]:
    """Liste triée des noms de stratégies disponibles."""
    import mt5.strategies as pkg

    names = [
        mod.name
        for mod in pkgutil.iter_modules(pkg.__path__)
        if mod.name not in _EXCLUDED and not mod.name.startswith("_")
    ]
    return sorted(names)


def get_strategy_class(name: str) -> type[Strategy]:
    """Résout la classe de stratégie nommée ``name`` (lève ``ValueError`` sinon)."""
    if not name or not name.replace("_", "").isalnum() or name in _EXCLUDED:
        raise ValueError(f"Nom de stratégie invalide : {name!r}")
    try:
        module = importlib.import_module(f"mt5.strategies.{name}")
    except ModuleNotFoundError as exc:
        available = ", ".join(list_strategies()) or "(aucune)"
        raise ValueError(f"Stratégie inconnue : {name!r}. Disponibles : {available}") from exc
    cls = getattr(module, "STRATEGY", None)
    if cls is None:
        candidates = [
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module.__name__
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"Le module {name!r} doit définir STRATEGY ou exactement une sous-classe de Strategy"
            )
        cls = candidates[0]
    if not (inspect.isclass(cls) and issubclass(cls, Strategy)):
        raise ValueError(f"STRATEGY dans {name!r} n'est pas une sous-classe de Strategy")
    return cls


def get_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    """Instancie la stratégie ``name`` avec ``params`` (fusionnés aux défauts)."""
    cls = get_strategy_class(name)
    instance = cls(params or {})
    if not getattr(instance, "name", None) or instance.name == "base":
        instance.name = name
    return instance


__all__ = ["get_strategy", "get_strategy_class", "list_strategies"]
