"""togeari run stops the machine when adding its report fails; conform takes a step budget."""

import argparse
import io
from types import SimpleNamespace

import pytest

from togeari.cli.commands import conform, run
from togeari.conformance.replay import STEP_BUDGET


class FakeMachine:
    def __init__(self):
        self.stopped = False

    def command(self, name, **kwargs):
        raise RuntimeError("plugin_add failed")

    def stop(self):
        self.stopped = True


def test_run_stops_the_machine_when_adding_the_report_fails(monkeypatch, tmp_path):
    machine = FakeMachine()
    monkeypatch.setattr(run, "build", lambda path: (machine, object(), object()))
    args = SimpleNamespace(machine="m.py", limit=None, report=str(tmp_path / "r"))
    with pytest.raises(RuntimeError):
        run.run(args, io.StringIO())
    assert machine.stopped


def test_conform_passes_its_step_budget_through(monkeypatch):
    seen = {}

    def fake_replay_dir(path, libs, runs, reexecute, step_budget):
        seen["budget"] = step_budget
        return iter(())

    monkeypatch.setattr(conform, "replay_dir", fake_replay_dir)
    parser = argparse.ArgumentParser()
    conform.add(parser.add_subparsers())
    args = parser.parse_args(["conform", "records", "--step-budget", "5000"])
    conform.run(args, io.StringIO())
    assert seen["budget"] == 5000

    default = parser.parse_args(["conform", "records"])
    assert default.step_budget == STEP_BUDGET
