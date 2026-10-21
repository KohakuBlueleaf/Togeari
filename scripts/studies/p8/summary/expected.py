"""The shapes a run is expected to measure, from the benches' own plans (independent of what the
run produced): a shape the run did not produce is then missing, never absent."""

import importlib.util
from pathlib import Path

from p6.scales import SCALES as P6_SCALES
from p6.studies import STUDIES as P6_STUDIES

from p8.scales import SCALES as GIL_SCALES

BENCHES = Path(__file__).resolve().parents[3] / "benches"


def _bench(name: str):
    spec = importlib.util.spec_from_file_location(f"p8_expected_{name}", BENCHES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shapes(p6_scale: str, gil_scale: str) -> dict:
    """{"ceiling": [(shape, setting)], "scaling": ["<shape> <n>"], "p6": {study: [name]},
    "gil": [(work / isolation, load)]} (the GIL idle load left out: no dispatcher work)."""
    ceiling = _bench("dispatcher_ceiling")
    scaling = _bench("scaling")
    gil = GIL_SCALES[gil_scale]
    p6 = {}
    for study in ("A", "B"):
        names = [c["name"] for c in P6_STUDIES[study].plan(P6_SCALES[p6_scale])]
        p6[study] = list(dict.fromkeys(names))
    return {
        "ceiling": [(s, setting) for s in ceiling.SHAPES for setting in ceiling.SETTINGS],
        "scaling": [f"{shape} {n}" for shape, sizes in scaling.SIZES.items() for n in sizes],
        "p6": p6,
        "gil": [
            (f"{w} / {i}", load)
            for w in gil.works
            for i in gil.isolations
            for load, _ in gil.loads
            if load != "idle"
        ],
    }
