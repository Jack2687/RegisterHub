"""
Dynamic Site Adapters Auto-Discovery Package.
Automatically imports and registers all adapters inheriting from BaseSiteAdapter.
"""
import importlib
import inspect
import re
from pathlib import Path
from typing import Dict, Type
from core.adapter import BaseSiteAdapter


def _class_to_slug(cls: Type[BaseSiteAdapter]) -> str:
    explicit = str(getattr(cls, "site_name", "") or "").strip().lower().replace("-", "_")
    if explicit:
        return explicit
    name = cls.__name__
    if name.endswith("Adapter") and len(name) > 7:
        name = name[:-7]
    slug = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return re.sub(r"[^a-z0-9_]+", "_", slug).strip("_")


def discover_adapters() -> Dict[str, Type[BaseSiteAdapter]]:
    """Scan the sites directory and return a map of site_name -> AdapterClass."""
    registry: Dict[str, Type[BaseSiteAdapter]] = {}
    sites_dir = Path(__file__).parent

    for py_file in sorted(sites_dir.glob("*.py")):
        if py_file.name.startswith("__") or py_file.name.startswith("_"):
            continue

        module_name = f"sites.{py_file.stem}"
        try:
            mod = importlib.import_module(module_name)
            defined = [
                obj
                for _, obj in inspect.getmembers(mod, inspect.isclass)
                if issubclass(obj, BaseSiteAdapter)
                and obj is not BaseSiteAdapter
                and getattr(obj, "__module__", "") == module_name
            ]
            if not defined:
                continue
            stem = py_file.stem.lower()
            primary = defined[0]
            registry[stem] = primary
            if stem.endswith("_site"):
                registry[stem[:-5]] = primary
            if len(defined) > 1:
                for cls in defined:
                    slug = _class_to_slug(cls)
                    if slug and slug not in registry:
                        registry[slug] = cls
        except Exception as e:
            # Log warning on syntax or import errors instead of silent drop
            import logging
            logging.getLogger("autoregister").warning(f"Failed to load adapter {py_file.name}: {e}")

    return registry
