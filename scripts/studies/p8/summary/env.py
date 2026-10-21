"""The environment of the S8 run: commit, interpreter, packages, CPU and the one CUDA device."""

import os
import platform
import subprocess
import sys
from pathlib import Path

import togeari
from togeari.core.construct.core import info as core_info


def _out(cmd: list[str], cwd: Path) -> str | None:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def collect(repo: Path) -> dict:
    sha = _out(["git", "rev-parse", "HEAD"], repo)
    dirty = _out(["git", "status", "--porcelain", "--untracked-files=no"], repo)
    # nvidia-smi lists NVIDIA devices only; index 0 is the device the studies run on
    gpu = _out(
        [
            "nvidia-smi",
            "--id=0",
            "--query-gpu=name,driver_version,memory.total,pci.bus_id",
            "--format=csv,noheader",
        ],
        repo,
    )
    return {
        "git_sha": sha.strip() if sha else None,
        "git_dirty_files": None if dirty is None else [x for x in dirty.splitlines() if x],
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "togeari": togeari.__version__,
        "core_info": core_info(),
        "gpu0": gpu.strip() if gpu else None,
        "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
        "switch_interval_s": sys.getswitchinterval(),
    }
