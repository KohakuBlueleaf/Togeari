"""togeari run stops the machine when adding its report fails; conform takes a step budget and
the engine-errors choice."""

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


def conform_with(monkeypatch, *argv: str) -> dict:
    """What `togeari conform records ARGV` passes to ``replay_dir``."""
    seen = {}

    def fake_replay_dir(path, libs, runs, reexecute, step_budget, engine_errors=None):
        seen.update(budget=step_budget, engine_errors=engine_errors)
        return iter(())

    monkeypatch.setattr(conform, "replay_dir", fake_replay_dir)
    parser = argparse.ArgumentParser()
    conform.add(parser.add_subparsers())
    conform.run(parser.parse_args(["conform", "records", *argv]), io.StringIO())
    return seen


def test_conform_passes_its_step_budget_through(monkeypatch):
    assert conform_with(monkeypatch, "--step-budget", "5000")["budget"] == 5000
    assert conform_with(monkeypatch)["budget"] == STEP_BUDGET


def test_conform_passes_engine_errors_through(monkeypatch):
    assert conform_with(monkeypatch)["engine_errors"] is True
    assert conform_with(monkeypatch, "--engine-errors")["engine_errors"] is True
    assert conform_with(monkeypatch, "--no-engine-errors")["engine_errors"] is False
