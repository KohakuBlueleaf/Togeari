"""Canonical digests of node inputs (conformance records, content store keys).

bytes and str hash directly; a value with ``__togeari_digest__`` uses it; a type
registered with ``register_digest`` uses its function (before the container cases, so a
registered list or dict subclass is digested by it); containers are digested recursively
(dict keys and set members in an order every process agrees on: strings by value, others by
their digest); arrays (``shape``, ``dtype``, ``tobytes``) hash dtype, shape and bytes;
anything else is pickled, with every set inside it pickled in that same order. Unpicklable
values digest to their type name and count as weak; so does a container met again inside
itself.
"""

import hashlib
import io
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

# Registration changes and cache fills; cache hits read without it
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

    # A fill and a registration never interleave, so no stale entry is cached
    with _LOCK:
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


class _OrderedSetPickler(pickle._Pickler):
    """A pickler that writes every set and frozenset with its members in ``_order``."""

    dispatch = pickle._Pickler.dispatch.copy()

    def _save_ordered(self, obj: Any) -> None:
        self.save_reduce(type(obj), (sorted(obj, key=_order),), obj=obj)

    dispatch[set] = _save_ordered
    dispatch[frozenset] = _save_ordered


# Protocol-5 opcodes that start a set or frozenset (EMPTY_SET, FROZENSET)
_SET_OPCODES = (pickle.EMPTY_SET, pickle.FROZENSET)


def _pickled(value: Any) -> bytes:
    """Pickled bytes the same in every process: the C pickler's, unless they may hold a set,
    then the ordered-set pickler's (a byte equal to a set opcode inside other data also takes
    that path; the choice depends on the value alone, so it is the same every time)."""
    data = pickle.dumps(value, protocol=5)
    if not any(op in data for op in _SET_OPCODES):
        return data
    buf = io.BytesIO()
    _OrderedSetPickler(buf, protocol=5).dump(value)
    return buf.getvalue()


def chunk(h, tag: bytes, data: bytes) -> None:
    """One self-delimiting piece: a tag, the data's length, the data."""
    h.update(tag + len(data).to_bytes(8, "little") + data)


def _weak(h, value: Any, weak: list) -> None:
    weak.append(type(value).__qualname__)
    chunk(h, b"w", f"{WEAK}{type(value).__qualname__}".encode())


def _feed(h, value: Any, weak: list, open_: set[int] | None = None) -> None:
    """Feed ``value`` into ``h``; ``open_`` holds the ids of the containers being fed."""
    match value:
        # Scalars
        case None | bool() | int() | float() | complex():
            chunk(h, b"v", f"{type(value).__name__}:{value!r}".encode())
        case str():
            chunk(h, b"s", value.encode("utf-8", "surrogatepass"))
        case bytes() | bytearray() | memoryview():
            chunk(h, b"b", bytes(value))

        # Values that digest themselves, then registered types
        case _ if hasattr(value, "__togeari_digest__"):
            chunk(h, b"d", value.__togeari_digest__())
        case _ if (fn := _registered(type(value))) is not None:
            chunk(h, b"r", fn(value))

        # Containers, recursively; a container met again inside itself is weak
        case list() | tuple() | dict() | set() | frozenset():
            open_ = set() if open_ is None else open_
            if id(value) in open_:
                _weak(h, value, weak)
                return
            open_.add(id(value))
            try:
                _feed_container(h, value, weak, open_)
            finally:
                open_.discard(id(value))

        # Arrays
        case _ if _is_array(value):
            chunk(h, b"a", f"{tuple(value.shape)}{value.dtype}".encode())
            chunk(h, b"A", value.tobytes())

        # Anything else: pickle, or the type name when it cannot be pickled
        case _:
            try:
                data = _pickled(value)
            except Exception:  # noqa: BLE001
                _weak(h, value, weak)
            else:
                chunk(h, b"p", data)


def _feed_container(h, value: Any, weak: list, inside: set[int]) -> None:
    match value:
        case list() | tuple():
            h.update(f"{type(value).__name__}[{len(value)}](".encode())
            for v in value:
                _feed(h, v, weak, inside)
        case dict():
            h.update(f"dict[{len(value)}](".encode())
            for k in sorted(value, key=_order):
                _feed(h, k, weak, inside)
                _feed(h, value[k], weak, inside)
        case _:
            h.update(f"set[{len(value)}](".encode())
            for v in sorted(value, key=_order):
                _feed(h, v, weak, inside)
    h.update(b")")


class _Tape:
    """Records what ``_feed`` writes."""

    __slots__ = ("parts",)

    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def update(self, data: bytes) -> None:
        self.parts.append(data)


def feed_bytes(value: Any, weak: list, open_: set[int] | None) -> bytes:
    """The bytes ``_feed`` writes for ``value`` (the native digest's path for every value it
    does not feed itself)."""
    tape = _Tape()
    _feed(tape, value, weak, open_)
    return b"".join(tape.parts)


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
