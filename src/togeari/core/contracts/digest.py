"""Canonical digests of node inputs (conformance records, content store keys).

bytes and str hash directly; containers are digested recursively (dict keys and set members
in an order every process agrees on: strings by value, others by their digest); a value with
``__togeari_digest__`` uses it; a type registered with ``register_digest`` uses
its function; arrays (``shape``, ``dtype``, ``tobytes``) hash dtype, shape and
bytes; anything else is pickled; unpicklable values digest to their type name
and count as weak.
"""

import hashlib
import pickle
import sys
import threading
from collections.abc import Callable
from typing import Any

WEAK = "weak:"

_REGISTRY: dict[type, Callable[[Any], bytes]] = {}
_BY_TYPE: dict[type, Callable[[Any], bytes] | None] = {}

# Modules behind each registration: the caller of register_digest and the function's module
_PROVIDERS: dict[type, frozenset[str]] = {}

# Registration changes; lookups read without it and never raise on a concurrent change
_LOCK = threading.Lock()
_UNSEEN = object()


def register_digest(cls: type, fn: Callable[[Any], bytes]) -> None:
    """Digest instances of ``cls`` (and its subclasses) by the bytes ``fn(value)`` returns.

    The bytes go into the hash as they are, so they should carry their own
    header (type, shape, ...) next to the content.
    """
    caller = sys._getframe(1).f_globals.get("__name__")
    modules = (caller, getattr(fn, "__module__", None))
    with _LOCK:
        _REGISTRY[cls] = fn
        _PROVIDERS[cls] = frozenset(m for m in modules if m is not None and m != "__main__")
        _BY_TYPE.clear()


def unregister_digest(cls: type) -> None:
    """Drop the digest registered for ``cls``, with its provider modules."""
    with _LOCK:
        _REGISTRY.pop(cls, None)
        _PROVIDERS.pop(cls, None)
        _BY_TYPE.clear()


def digest_providers() -> list[str]:
    """The sorted names of the modules behind the current registrations."""
    with _LOCK:
        return sorted(set().union(*_PROVIDERS.values()))


def _registered(t: type) -> Callable[[Any], bytes] | None:
    fn = _BY_TYPE.get(t, _UNSEEN)
    if fn is not _UNSEEN:
        return fn
    fn = next((f for c in t.__mro__ if (f := _REGISTRY.get(c)) is not None), None)
    _BY_TYPE[t] = fn
    return fn


def _order(key: Any) -> tuple:
    """A sort key of a dict key or set member that is the same in every process: strings by
    value, anything else by its own digest."""
    match key:
        case str():
            return (0, key)
        case _:
            return (1, digest(key))


def _is_array(value: Any) -> bool:
    return hasattr(value, "shape") and hasattr(value, "dtype") and hasattr(value, "tobytes")


def chunk(h, tag: bytes, data: bytes) -> None:
    """One self-delimiting piece: a tag, the data's length, the data."""
    h.update(tag + len(data).to_bytes(8, "little") + data)


def _feed(h, value: Any, weak: list) -> None:
    match value:
        # Scalars
        case None | bool() | int() | float() | complex():
            chunk(h, b"v", f"{type(value).__name__}:{value!r}".encode())
        case str():
            chunk(h, b"s", value.encode("utf-8", "surrogatepass"))
        case bytes() | bytearray() | memoryview():
            chunk(h, b"b", bytes(value))

        # Values that digest themselves
        case _ if hasattr(value, "__togeari_digest__"):
            chunk(h, b"d", value.__togeari_digest__())

        # Containers, recursively
        case list() | tuple():
            h.update(f"{type(value).__name__}[{len(value)}](".encode())
            for v in value:
                _feed(h, v, weak)
            h.update(b")")
        case dict():
            h.update(f"dict[{len(value)}](".encode())
            for k in sorted(value, key=_order):
                _feed(h, k, weak)
                _feed(h, value[k], weak)
            h.update(b")")
        case set() | frozenset():
            h.update(f"set[{len(value)}](".encode())
            for v in sorted(value, key=_order):
                _feed(h, v, weak)
            h.update(b")")

        # Registered types, then arrays
        case _ if (fn := _registered(type(value))) is not None:
            chunk(h, b"r", fn(value))
        case _ if _is_array(value):
            chunk(h, b"a", f"{tuple(value.shape)}{value.dtype}".encode())
            chunk(h, b"A", value.tobytes())

        # Anything else: pickle, or the type name when it cannot be pickled
        case _:
            try:
                data = pickle.dumps(value, protocol=5)
            except Exception:  # noqa: BLE001
                weak.append(type(value).__qualname__)
                chunk(h, b"w", f"{WEAK}{type(value).__qualname__}".encode())
            else:
                chunk(h, b"p", data)


def digest(value: Any) -> bytes:
    """sha256 digest of ``value``."""
    h = hashlib.sha256()
    _feed(h, value, [])
    return h.digest()


def digest_with_weak(value: Any) -> tuple[bytes, list[str]]:
    """The digest and the type names compared by type only."""
    h = hashlib.sha256()
    weak: list[str] = []
    _feed(h, value, weak)
    return h.digest(), weak
