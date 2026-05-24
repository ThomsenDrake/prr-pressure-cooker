from __future__ import annotations

import importlib
import inspect
import pkgutil

try:
    import mistralai.workflows as workflows
except ImportError:  # pragma: no cover - optional runtime dependency
    workflows = None


def discover_workflows() -> list[type]:
    if workflows is None:
        return []

    discovered: list[type] = []
    seen: set[type] = set()
    for module_info in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        for _name, value in inspect.getmembers(module, inspect.isclass):
            if value in seen:
                continue
            try:
                definition = workflows.get_workflow_definition(value)
            except Exception:
                definition = None
            if definition is not None:
                discovered.append(value)
                seen.add(value)
    return discovered


WORKFLOW_CLASSES = discover_workflows()

__all__ = ["WORKFLOW_CLASSES", "discover_workflows"]
