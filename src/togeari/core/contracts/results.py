"""What an engine hands back for one work item."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Ok:
    """The item ran; ``value`` is the raw reply, or the node result once finished."""

    value: Any
    finished: bool = False


@dataclass(frozen=True)
class ItemError:
    """This item failed (its input, a timeout on it, a crash while running it)."""

    error: BaseException


@dataclass(frozen=True)
class UnitError:
    """The whole unit is blocked; the lane takes it out of service."""

    error: BaseException
    retry_after: float | None = None


@dataclass(frozen=True)
class Cancelled:
    """The item was cancelled before it produced a result."""


Result = Ok | ItemError | UnitError | Cancelled


class UnitUnavailable(RuntimeError):
    """No unit could serve the item within its retries."""


class ItemTimeout(TimeoutError):
    """A call or attempt deadline expired (also usable as an ordinary host exception)."""


class Blocked(RuntimeError):
    """A plugin blocked the item (``PluginBlockError`` in ``pre_submit``)."""
