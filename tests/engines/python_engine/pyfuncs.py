"""Top-level node functions for the PythonCallEngine checks (importable by worker processes)."""

import os
import threading
import time

# Opens ``gated`` calls (thread mode: one process).
GATE = threading.Event()


def double(x):
    return 2 * x


def fail(x):
    raise ValueError(f"cannot take {x}")


def die(x):
    os._exit(3)


def echo(blob):
    return blob


def gated(x):
    GATE.wait(60.0)
    return x


def nap(x, ms):
    time.sleep(ms / 1000.0)
    return x
