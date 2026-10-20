"""Every example runs to its end and every job matches the reference interpreter.

Each example exits nonzero when a job fails or does not match the reference; each runs in its
own interpreter, as a user runs it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_example(name: str, *args: str, timeout: float = 120.0) -> str:
    res = subprocess.run(
        [sys.executable, str(ROOT / "examples" / name / "machine.py"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert res.returncode == 0, f"{name} exited {res.returncode}\n{res.stdout}\n{res.stderr}"
    return res.stdout


@pytest.mark.parametrize("name", ["hello", "multi_unit", "trace_replay", "parallel"])
def test_a_cpu_example_conforms(name):
    out = run_example(name)
    assert "jobs match the reference" in out


@pytest.mark.gpu
def test_the_mixed_example_conforms_on_twelve_stations():
    out = run_example("mixed", "12", timeout=600.0)
    assert "conformance: 12/12 jobs match the reference" in out
    assert "ordered log: 0 problem(s)" in out
