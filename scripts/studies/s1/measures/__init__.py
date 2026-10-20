"""The five measurements. Each module has ``NAME``, ``TITLE``, ``plan(scale)`` (its configs,
each a dict with a unique ``name``) and ``run(config, scale)`` (one config's result dict).

Modules load by name, so a child process imports only the measurement it runs.
"""

import importlib
from types import ModuleType

MEASURES = {
    "m1": "s1.measures.m1_transport",
    "m2": "s1.measures.m2_torch",
    "m3": "s1.measures.m3_gpu_share",
    "m4": "s1.measures.m4_python",
    "m5": "s1.measures.m5_http",
}


def load(name: str) -> ModuleType:
    return importlib.import_module(MEASURES[name])
