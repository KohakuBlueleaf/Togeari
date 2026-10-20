"""The ceiling bench shapes and the example machine modules, loaded by path."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[4]
BENCHES = REPO / "scripts" / "benches"
EXAMPLES = REPO / "examples"

sys.path.insert(0, str(BENCHES))

import dispatcher_ceiling as ceiling  # noqa: E402

_loaded: dict[str, ModuleType] = {}


def example(name: str) -> ModuleType:
    """``examples/<name>/machine.py`` as a module, loaded once."""
    if name not in _loaded:
        path = EXAMPLES / name / "machine.py"
        spec = importlib.util.spec_from_file_location(f"p6_example_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _loaded[name] = module
    return _loaded[name]


def ceiling_jobs(name: str, share: float) -> list:
    """The first ``share`` of a ceiling shape's jobs (at least one)."""
    jobs = ceiling.SHAPES[name][2]
    return jobs[: max(1, int(len(jobs) * share))]
