"""Value helpers of the runtime: copies for `mutates` and records, size estimates for admission."""

import sys
from typing import Any

from togeari.core.contracts.io import copy_value

# Container items looked at when estimating a container's size (the rest are scaled from them)
_SAMPLE = 64


def kept_copy(value: Any) -> tuple[Any, bool]:
    """(a copy no later in-place change reaches, True), or (value, False) when it cannot copy."""
    try:
        return copy_value(value), True
    except Exception:  # noqa: BLE001
        return value, False


def value_bytes(value: Any, depth: int = 2) -> int:
    """Estimated bytes held by ``value``: buffer sizes, string lengths, sampled container items."""
    match value:
        case None | bool() | int() | float():
            return 0
        case str() | bytes() | bytearray():
            return len(value)
        case memoryview():
            return value.nbytes

        # Containers: the first items sampled, scaled to the length
        case list() | tuple() | set() | frozenset() | dict() if depth > 0:
            items = value.values() if isinstance(value, dict) else value
            n = len(value)
            total = 0
            for i, v in enumerate(items):
                if i >= _SAMPLE:
                    break
                total += value_bytes(v, depth - 1)
            if n > _SAMPLE:
                total = total * n // _SAMPLE
            return sys.getsizeof(value) + total

        # Buffers by ``nbytes``, the rest by ``getsizeof``
        case _:
            nbytes = getattr(value, "nbytes", None)
            if isinstance(nbytes, int):
                return nbytes
            try:
                return sys.getsizeof(value)
            except Exception:  # noqa: BLE001
                return 0
