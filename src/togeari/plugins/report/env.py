"""The environment of a run: interpreter, OS, package versions."""

import os
import platform
import sys
from importlib import metadata

PACKAGES = (
    "togeari",
    "kohakunode",
    "torch",
    "numpy",
    "httpx",
    "sniffio",
    "kohakuvault",
    "zstandard",
)


def version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def environment() -> dict:
    return {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "os": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpus": os.cpu_count(),
        "pid": os.getpid(),
        "packages": {p: version(p) for p in PACKAGES},
    }
