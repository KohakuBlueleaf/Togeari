"""``togeari doctor``: versions, devices, and engine worker processes left over from
earlier runs.
"""

import argparse
import json
import subprocess
import sys
from typing import TextIO

from togeari.engines.common import registry
from togeari.plugins.report.env import environment

# Entry points of the builtin engines' worker processes.
WORKER_ENTRIES = ("togeari.engines.python.child", "togeari.engines.torch.child")

DEVICES = """
import json
try:
    import torch
except ImportError:
    print(json.dumps({"torch": None}))
else:
    cuda = torch.cuda.is_available()
    devs = []
    for i in range(torch.cuda.device_count() if cuda else 0):
        p = torch.cuda.get_device_properties(i)
        free, total = torch.cuda.mem_get_info(i)
        devs.append(
            {"index": i, "name": p.name, "total_gb": total / 2**30, "free_gb": free / 2**30}
        )
    print(json.dumps({"torch": torch.__version__, "cuda": torch.version.cuda, "devices": devs}))
"""

WINDOWS_PROCESSES = (
    "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
    'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
)


def add(sub: argparse._SubParsersAction) -> None:
    sub.add_parser("doctor", help="versions, devices, leftover worker processes")


def devices(timeout: float = 120.0) -> dict:
    """Devices as torch sees them, asked in a child interpreter."""
    try:
        res = subprocess.run(
            [sys.executable, "-c", DEVICES], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {"error": f"no answer in {timeout}s"}
    if res.returncode != 0:
        return {"error": res.stderr.strip().splitlines()[-1:] or ["failed"]}
    return json.loads(res.stdout.strip().splitlines()[-1])


def processes() -> dict[int, str]:
    """pid -> command line of every process the OS lists."""
    match sys.platform:
        case "win32":
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PROCESSES]
        case _:
            # Unlimited width: ps cuts lines at 80 columns when not writing to a terminal
            cmd = ["ps", "-eww", "-o", "pid=,args="]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found = {}
    for line in res.stdout.splitlines():
        pid, _, cmdline = line.strip().replace("\t", " ", 1).partition(" ")
        if pid.isdigit():
            found[int(pid)] = cmdline.strip()
    return found


def worker_processes(procs: dict[int, str]) -> tuple[list[dict], list[dict]]:
    """Registered engine workers that still run: (leftover, live).

    A worker counts when its pid runs a spawned child process and its registry
    entry names a builtin worker entry point (``WORKER_ENTRIES``); it is
    leftover when its parent process has exited, live otherwise. Registry files
    of processes that no longer exist are removed.
    """
    leftover, live = [], []
    for info in registry.workers():
        pid = info.get("pid")
        cmdline = procs.get(pid)
        if cmdline is None:
            registry.forget(pid)
            continue
        if info.get("entry") not in WORKER_ENTRIES or "multiprocessing" not in cmdline.lower():
            continue
        row = {**info, "cmdline": cmdline}
        (live if info.get("parent") in procs else leftover).append(row)
    return leftover, live


def run(args: argparse.Namespace, out: TextIO) -> int:
    env = environment()
    print(f"python   {env['python'].split()[0]}  ({env['executable']})", file=out)
    print(f"os       {env['os']}  cpus {env['cpus']}", file=out)
    for name, v in env["packages"].items():
        print(f"{name:<12} {v or '-'}", file=out)

    # Devices
    dev = devices()
    match dev:
        case {"torch": None}:
            print("devices  torch not installed", file=out)
        case {"error": err}:
            print(f"devices  error: {err}", file=out)
        case _:
            print(f"devices  torch {dev['torch']} cuda {dev['cuda']}", file=out)
            for d in dev["devices"]:
                print(
                    f"  cuda:{d['index']}  {d['name']}"
                    f"  {d['free_gb']:.1f}/{d['total_gb']:.1f} GB free",
                    file=out,
                )

    # Engine worker processes: leftover ones (parent exited) are listed
    leftover, live = worker_processes(processes())
    print(
        f"workers  {len(leftover)} leftover togeari worker process(es);"
        f" {len(live)} of running machines",
        file=out,
    )
    for w in leftover:
        print(f"  {w['pid']}  {w['entry']}  parent {w['parent']} exited", file=out)
    return 0
