"""Loading what the CLI is pointed at: machine modules and ``module:attr`` references."""

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

from togeari.core.construct.machine import Machine


def resolve(ref: str) -> Any:
    """The object named by ``module:attr`` (``attr`` may be dotted)."""
    mod_name, _, attr = ref.partition(":")
    if not attr:
        raise ValueError(f"{ref!r} is not module:attr")
    obj: Any = importlib.import_module(mod_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def load_module(path: str | Path) -> Any:
    """Import a Python file as a module; its folder goes on ``sys.path``."""
    path = Path(path).resolve()
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    name = f"togeari_machine_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module

    # The file's own name too, so records name its libraries as an importable module
    if path.stem.isidentifier() and path.stem not in sys.modules:
        sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def _attr(module: Any, name: str) -> Any:
    v = getattr(module, name, None)
    return v() if callable(v) and not hasattr(v, "__iter__") and not hasattr(v, "write") else v


def accepted(fn: Any, options: dict) -> dict:
    """The options ``fn`` takes as keyword arguments (all of them with ``**kwargs``)."""
    params = inspect.signature(fn).parameters.values()
    if any(p.kind is p.VAR_KEYWORD for p in params):
        return dict(options)
    names = {p.name for p in params if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)}
    return {k: v for k, v in options.items() if k in names}


def build_module(path: str | Path, **options: Any) -> tuple[Any, Machine, Any, Any]:
    """(module, machine, source, sink) from a module's ``build()``.

    ``build()`` returns ``(machine, source, sink)``, or a machine alone with the
    module's ``source`` and ``sink`` (values, or functions returning them).
    Each of ``options`` is passed when ``build()`` takes it as a keyword.
    """
    module = load_module(path)
    fn = getattr(module, "build", None)
    if fn is None:
        raise AttributeError(f"{path} defines no build()")
    match fn(**accepted(fn, options)):
        case (Machine() as machine, source, sink):
            return module, machine, source, sink
        case Machine() as machine:
            return module, machine, _attr(module, "source"), _attr(module, "sink")
        case other:
            raise TypeError(f"build() returned {type(other).__name__}, not a Machine")


def build(path: str | Path, **options: Any) -> tuple[Machine, Any, Any]:
    _, machine, source, sink = build_module(path, **options)
    return machine, source, sink
