"""Which core runs a machine (spec/11 §11.2).

The Python runtime is the reference; the Rust core is ``togeari._rs``. ``"auto"`` takes
``AUTO`` (the core the measurements of spec/11 §11.6 chose). ``TOGEARI_CORE`` sets the default
for a machine built without ``core=``.
"""

import os
from types import ModuleType

try:
    from togeari import _rs
except ImportError:
    _rs = None

CORES = ("auto", "rust", "python")
ENV = "TOGEARI_CORE"

# The core "auto" runs when it is built (spec/11 §11.6); Python otherwise
AUTO = "rust"


def native() -> ModuleType | None:
    """``togeari._rs``, or None when it is not built."""
    return _rs


def rust_available() -> bool:
    """The extension imports and holds the native dispatcher."""
    return _rs is not None and bool(_rs.DISPATCHER)


def info() -> dict:
    """The core a default machine runs, and the extension: ``default``, ``resolved``,
    ``extension`` (``core_info()``, or None when not built)."""
    default = os.environ.get(ENV) or "auto"
    resolved, problem = resolve(None)
    return {
        "default": default,
        "resolved": resolved if problem is None else f"error: {problem}",
        "extension": None if _rs is None else dict(_rs.core_info()),
    }


def resolve(core: str | None) -> tuple[str, str | None]:
    """(the core that runs, a construction problem or None) for ``Machine(core=core)``."""
    asked = core if core is not None else os.environ.get(ENV) or "auto"
    source = "core=" if core is not None else f"{ENV}="
    match asked:
        case "python":
            return "python", None
        case "auto":
            return ("rust" if AUTO == "rust" and rust_available() else "python"), None
        case "rust" if rust_available():
            return "rust", None
        case "rust" if _rs is None:
            return "python", f'{source}"rust" but togeari._rs is not built'
        case "rust":
            return "python", f'{source}"rust" but togeari._rs {_rs.__version__} has no dispatcher'
        case _:
            return "python", f"{source}{asked!r} is not one of {', '.join(CORES)}"
