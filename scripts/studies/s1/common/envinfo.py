"""The environment of the run: interpreter, platform, CPU, library versions, the CUDA device."""

import os
import platform
import sys

import httpx
import numpy as np
import torch

import togeari
from s1.common.stats import psutil


def collect() -> dict:
    info = {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "togeari": togeari.__version__,
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "cudnn": torch.backends.cudnn.version(),
        "numpy": np.__version__,
        "httpx": httpx.__version__,
        "psutil": None if psutil is None else psutil.__version__,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
        "switch_interval_s": sys.getswitchinterval(),
        "gpu": None,
    }

    # The first visible device
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        info["gpu"] = {
            "count_visible": torch.cuda.device_count(),
            "name": props.name,
            "total_mb": props.total_memory / 2**20,
            "capability": f"{props.major}.{props.minor}",
            "multiprocessors": props.multi_processor_count,
        }
    return info
