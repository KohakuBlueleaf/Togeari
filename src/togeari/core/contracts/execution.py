"""Execution controls resolved by bindings, separate from semantic program hints."""

import math
from dataclasses import dataclass
from numbers import Real


@dataclass(frozen=True, slots=True, init=False)
class ExecutionPolicy:
    """Per-call execution controls for lane-bound work.

    ``timeout_s=None`` means no deadline; zero means an immediate deadline. A
    timeout must be a finite, nonnegative real number (not a boolean or string).
    Explicit empty policies disable legacy timeout hints. Inline calls have no
    policy argument: dispatcher execution cannot be interrupted by a lane deadline.
    """

    timeout_s: float | None = None

    def __init__(self, timeout_s: float | None = None) -> None:
        try:
            _ = self.timeout_s
        except AttributeError:
            pass
        else:
            raise TypeError("immutable ExecutionPolicy cannot be reinitialized")
        timeout = timeout_s
        if timeout is None:
            object.__setattr__(self, "timeout_s", None)
            return
        if isinstance(timeout, bool) or not isinstance(timeout, Real):
            raise TypeError("ExecutionPolicy.timeout_s must be a real number or None")
        # Reject negative reals before conversion can underflow them to -0.0.
        if timeout < 0:
            raise ValueError("ExecutionPolicy.timeout_s must be finite and nonnegative")
        try:
            normalized = float(timeout)
        except (OverflowError, ValueError) as exc:
            raise ValueError("ExecutionPolicy.timeout_s must be finite and nonnegative") from exc
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError("ExecutionPolicy.timeout_s must be finite and nonnegative")
        object.__setattr__(self, "timeout_s", normalized)

    def __setstate__(self, state) -> None:
        raise TypeError("immutable ExecutionPolicy cannot be replaced")

    def __reduce__(self):
        return ExecutionPolicy, (self.timeout_s,)
