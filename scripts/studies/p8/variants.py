"""The variants the P8 studies compare: a core and the knobs set on every machine of the run.

``python`` is the reference core; ``rust-o<us>`` the native core with ``gil_offer_us`` at
``<us>`` (the least time on the core's clock between two GIL offers): ``rust-oinf`` runs every
region held and never offers, ``rust-onone`` runs no region and never offers. Both still release
the GIL while waiting for events.
"""

import json
import math

# The offer intervals swept, in microseconds, in sweep order (the decision's last tie-break)
OFFERS: tuple[float | None, ...] = (0, 20, 100, 300, 1000, 5000, math.inf, None)


def offer_name(us: float | None) -> str:
    return "rust-onone" if us is None else f"rust-o{us:g}"


VARIANTS: dict[str, dict] = {
    "python": {"core": "python", "knobs": {}},
    **{offer_name(us): {"core": "rust", "knobs": {"gil_offer_us": us}} for us in OFFERS},
}
ALL = ",".join(VARIANTS)


def pick(names: str) -> dict[str, dict]:
    """The variants named in a comma-separated list, in its order."""
    chosen = [n for n in names.split(",") if n]
    unknown = [n for n in chosen if n not in VARIANTS]
    if unknown:
        raise SystemExit(f"unknown variants {unknown}; known: {list(VARIANTS)}")
    return {n: VARIANTS[n] for n in chosen}


def rotated(names: list[str], rnd: int) -> list[str]:
    """``names`` rotated by ``rnd``: round ``rnd`` starts with the next variant."""
    k = rnd % len(names) if names else 0
    return names[k:] + names[:k]


def knobs_arg(variant: dict) -> list[str]:
    """The ``--knobs`` argument of a bench run for ``variant``."""
    return ["--knobs", json.dumps(variant["knobs"])]
