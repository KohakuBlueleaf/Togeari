"""KIR rules the runtime keeps: undefined values, runtime faults, literal copies."""

import copy
from typing import Any

from togeari.core.runtime.state import Slot, SlotState


class _Undefined:
    """The one undefined value; pickles and copies as itself."""

    __slots__ = ()

    def __reduce__(self) -> str:
        return "UNDEF"

    def __repr__(self) -> str:
        return "UNDEF"


# Value of a name no path wrote; reading it is a runtime error
UNDEF = _Undefined()


class RuntimeFault(Exception):
    """A KIR runtime error (not catchable by `@try`): bad condition, missing name, ..."""


def undefined() -> Slot:
    """A fresh slot holding ``UNDEF`` (one per resolution, never shared between jobs)."""
    return Slot(UNDEF, SlotState.READY)


def fresh(value: Any) -> Any:
    """Literal containers are copied per use, like the reference interpreter."""
    return copy.deepcopy(value) if isinstance(value, (list, dict, set)) else value
