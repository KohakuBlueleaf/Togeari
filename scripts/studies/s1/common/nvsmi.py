"""Device-wide samples from ``nvidia-smi`` in the parent: memory used, utilization, per-process use.

The parent samples while a child runs, so the sampling costs nothing in the
measured process. Samples carry epoch times; ``window`` cuts them per phase.
"""

import shutil
import subprocess
import threading
import time

GPU_QUERY = "index,name,memory.used,memory.total,utilization.gpu"
APP_QUERY = "pid,used_memory"


def _query(args: list[str]) -> list[list[str]]:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return []
    done = subprocess.run(
        [exe, *args, "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return [[c.strip() for c in line.split(",")] for line in done.stdout.splitlines() if line]


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def sample(index: int = 0) -> dict | None:
    """One device-wide sample of GPU ``index``; None without ``nvidia-smi``."""
    t = time.time()
    rows = [r for r in _query([f"--query-gpu={GPU_QUERY}"]) if r and r[0] == str(index)]
    if not rows:
        return None
    _, name, used, total, util = rows[0][:5]
    apps = _query([f"--query-compute-apps={APP_QUERY}", f"--id={index}"])
    return {
        "epoch": t,
        "name": name,
        "used_mb": _number(used),
        "total_mb": _number(total),
        "util_pct": _number(util),
        "apps": [{"pid": r[0], "used_mb": _number(r[1])} for r in apps if len(r) >= 2],
    }


class Sampler:
    """Samples GPU ``index`` every ``interval_s`` on a thread until ``stop()``."""

    def __init__(self, interval_s: float, index: int = 0):
        self.interval_s = interval_s
        self.index = index
        self.samples: list[dict] = []
        self.halt = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="nvsmi", daemon=True)

    def start(self) -> "Sampler":
        self.thread.start()
        return self

    def _loop(self) -> None:
        while not self.halt.is_set():
            got = sample(self.index)
            if got is not None:
                self.samples.append(got)
            self.halt.wait(self.interval_s)

    def stop(self) -> list[dict]:
        self.halt.set()
        self.thread.join(15.0)
        return self.samples


def window(samples: list[dict], t0: float, t1: float, base_mb: float | None) -> dict:
    """Device memory and utilization over the samples taken in ``[t0, t1]`` (epoch)."""
    inside = [s for s in samples if t0 <= s["epoch"] <= t1]
    used = [s["used_mb"] for s in inside if s["used_mb"] is not None]
    util = [s["util_pct"] for s in inside if s["util_pct"] is not None]
    return {
        "samples": len(inside),
        "used_mb_max": max(used) if used else None,
        "used_mb_over_base_max": (max(used) - base_mb) if used and base_mb is not None else None,
        "util_pct_mean": sum(util) / len(util) if util else None,
        "util_pct_max": max(util) if util else None,
    }
