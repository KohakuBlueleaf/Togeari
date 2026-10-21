"""The knob registry (spec/05 §5.6): initial values and the check every change goes through."""

from numbers import Real
from typing import Any

from togeari.core.observe.bus import LEVELS

KNOB_DEFAULTS: dict[str, Any] = {
    "window": 64,
    "retire_buffer": 4096,
    "run_ahead": True,
    "mutates": "copy-if-speculative",
    "inline_warn_us": 50,
    "snapshot_every": None,
    "record": None,
    "record_limit": 1_000_000,
    "events": "sampled",
    "event_sample": 0.1,
    "fingerprint_inputs": False,
    "assert_alias": False,
    "step_budget": None,
    "plugin_errors": "disable",
    "gc_threshold": (50_000, 20, 100),
    "squash_reuse": 1024,
    "squash_reuse_bytes": 64 << 20,
    "gil_offer_us": 0,
}
POLICY_KNOBS = ("admission", "issue")
MUTATES_MODES = ("copy-if-speculative", "wait", "copy-always")
PLUGIN_ERRORS = ("disable", "log")


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v: Any) -> bool:
    """A real number the cores can read as a float (an int past the float range is not)."""
    if not isinstance(v, Real) or isinstance(v, bool):
        return False
    try:
        float(v)
    except OverflowError:
        return False
    return True


def _problem(name: str, v: Any) -> str | None:
    match name:
        # Counts and switches
        case "window" | "retire_buffer":
            ok = _int(v) and v >= 1
            want = "an int >= 1"
        case "snapshot_every" | "step_budget" | "record_limit":
            ok = v is None or (_int(v) and v >= 1)
            want = "None or an int >= 1"
        case "run_ahead" | "fingerprint_inputs" | "assert_alias":
            ok = isinstance(v, bool)
            want = "a bool"

        # Named modes
        case "mutates":
            ok = v in MUTATES_MODES
            want = f"one of {MUTATES_MODES}"
        case "events":
            ok = v in LEVELS
            want = f"one of {LEVELS}"
        case "plugin_errors":
            ok = v in PLUGIN_ERRORS
            want = f"one of {PLUGIN_ERRORS}"

        # Bounds and rates
        case "squash_reuse":
            ok = _int(v) and v >= 0
            want = "an int >= 0 (0 turns reuse off)"
        case "squash_reuse_bytes":
            ok = v is None or (_int(v) and v >= 0)
            want = "None (no byte cap) or an int >= 0"
        case "inline_warn_us" | "gil_offer_us":
            ok = v is None or (_num(v) and v >= 0)
            want = "None or a number >= 0 within the float range (inf included)"
        case "record":
            ok = v is None or (_num(v) and 0 <= v <= 1)
            want = "None or a rate in [0, 1]"
        case "event_sample":
            ok = _num(v) and 0 <= v <= 1
            want = "a rate in [0, 1]"

        # The collector ask
        case "gc_threshold":
            ok = v is None or (
                isinstance(v, tuple)
                and 1 <= len(v) <= 3
                and all(_int(n) and n >= 0 for n in v)
                and v[0] >= 1
            )
            want = (
                "None or a tuple of 1 to 3 ints: the first >= 1 (0 would stop the collector),"
                " the others >= 0"
            )

        # Policies
        case "admission":
            ok = callable(getattr(v, "allows", None))
            want = "an admission rule (allows) or a contributed policy's name"
        case "issue":
            ok = callable(getattr(v, "order", None)) and callable(getattr(v, "priority", None))
            want = "an issue order (order, priority) or a contributed policy's name"

        case _:
            return f"unknown knob {name!r}"
    return None if ok else f"{name} must be {want}, not {v!r}"


def checked(name: str, value: Any, policies: dict) -> tuple[Any, str | None]:
    """(the value to store, a problem or None); policy knobs take contributed policies by name."""
    if name in POLICY_KNOBS and isinstance(value, str):
        if value not in policies:
            return value, f"{name}: no contributed policy {value!r}"
        value = policies[value]
    if name == "gc_threshold" and isinstance(value, list):
        value = tuple(value)
    return value, _problem(name, value)
