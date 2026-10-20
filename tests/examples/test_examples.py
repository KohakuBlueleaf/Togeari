"""Every example runs to its end and every source item ends in a job matching the reference
interpreter.

Each example exits nonzero when a job fails, does not match the reference, or is lost; each runs
in its own interpreter, as a user runs it.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_example(name: str):
    spec = importlib.util.spec_from_file_location(
        f"example_{name}", ROOT / "examples" / name / "machine.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@pytest.mark.parametrize(
    ("name", "lines"),
    [
        ("hello", ["conformance: 5/5 "]),
        ("multi_unit", ["conformance: 40/40 "]),
        ("trace_replay", ["conformance: 60/60 "]),
        ("parallel", ["conformance: 1/1 ", "conformance: 4/4 "]),
    ],
)
def test_a_cpu_example_conforms_on_every_source_item(name, lines):
    out = run_example(name)
    for line in lines:
        assert line in out


def test_a_lost_job_is_a_problem():
    hello = load_example("hello")
    machine, source, sink = hello.build()
    with machine:
        machine.run(source, sink)
        job_program = machine.code.entries["greet"].region.job

    # One record dropped after the run: its source item is reported
    kept = [rec for rec in sink.records if rec.key != "Eli"]
    inputs = {p["name"]: p for p in hello.PEOPLE}
    assert hello.verify(job_program, sink.records, inputs) == []
    assert hello.verify(job_program, kept, inputs) == ["Eli: no record"]


@pytest.mark.gpu
def test_the_mixed_example_conforms_on_twelve_stations():
    out = run_example("mixed", "12", timeout=600.0)
    assert "conformance: 12/12 jobs match the reference" in out
    assert "ordered log: 0 problem(s)" in out
