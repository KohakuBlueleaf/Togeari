"""togeari doctor when the OS process listing fails: nothing is forgotten, the command fails."""

import io
import json
import subprocess
import time

from togeari.cli.commands import doctor
from togeari.engines.common import registry


def _register(pid: int, parent: int) -> None:
    info = {"pid": pid, "parent": parent, "entry": doctor.WORKER_ENTRIES[0], "started": time.time()}
    (registry.REGISTRY / f"{pid}.json").write_text(json.dumps(info), encoding="utf-8")


def failing_run(*args, **kwargs):
    return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="listing failed")


def test_a_failed_listing_forgets_no_worker_and_the_command_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY", tmp_path)
    monkeypatch.setattr(doctor.subprocess, "run", failing_run)
    monkeypatch.setattr(doctor, "devices", lambda timeout=120.0: {"torch": None})
    _register(4242, 1)

    assert doctor.processes() is None
    assert doctor.worker_processes(None) == ([], [])
    out = io.StringIO()
    assert doctor.run(None, out) == 1
    assert "listing failed" in out.getvalue()
    assert (tmp_path / "4242.json").exists()


def test_a_listing_that_raises_is_a_failed_listing(monkeypatch):
    def raising(*args, **kwargs):
        raise OSError("no ps")

    monkeypatch.setattr(doctor.subprocess, "run", raising)
    assert doctor.processes() is None
