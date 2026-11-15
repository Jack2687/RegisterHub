"""
Dynamic Site Adapters Auto-Discovery Package.
Automatically imports and registers all adapters inheriting from BaseSiteAdapter.
"""
import importlib
import inspect
from pathlib import Path
from typing import Dict, Type
from core.adapter import BaseSiteAdapter


def discover_adapters() -> Dict[str, Type[BaseSiteAdapter]]:
    """Scan the sites directory and return a map of site_name -> AdapterClass."""
    registry: Dict[str, Type[BaseSiteAdapter]] = {}
    sites_dir = Path(__file__).parent

    for py_file in sites_dir.glob("*.py"):
        if py_file.name.startswith("__") or py_file.name.startswith("_"):
            continue

        module_name = f"sites.{py_file.stem}"
        try:
            mod = importlib.import_module(module_name)
            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if issubclass(obj, BaseSiteAdapter) and obj is not BaseSiteAdapter:
                    # Register by module stem name (e.g. demo_site -> demo_site, demo -> demo)
                    key = py_file.stem.lower().replace("_site", "")
                    registry[key] = obj
                    registry[py_file.stem.lower()] = obj
                    break
        except Exception as e:
            # Silent skip on syntax or import errors in unfinished draft adapters
            pass

    return registry
