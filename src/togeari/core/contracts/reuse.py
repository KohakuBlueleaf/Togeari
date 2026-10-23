"""Maintained exact builtin recognition shared by binding and execution manifests.

A signature/library spelling alone proves nothing about an implementation. These references
are captured once; replacements and RecordOperation subclasses do not inherit recognition.
Bump maintained revisions when the corresponding implementation semantics change.
"""

from typing import Any

from kohakunode.records import RecordOperation
from kohakunode.sig.env import Resolved
from kohakunode.sig.std import INTERNAL, STD

_BUILTINS = {
    (id(lib), name): (fn, f"{lib.name}.{name}/1")
    for lib in (STD, INTERNAL)
    for name, fn in lib.impls.items()
}
_RECORD_REVISION = "kir.RecordOperation/1"


def builtin_revision(resolved: Any, fn: Any) -> str | None:
    """Recognize an exact maintained host implementation, not arbitrary Python code."""
    if type(fn) is RecordOperation:
        return _RECORD_REVISION
    if isinstance(resolved, Resolved):
        known = _BUILTINS.get((id(resolved.library), resolved.signature.name))
        if known is not None and known[0] is fn:
            return known[1]
    return None
