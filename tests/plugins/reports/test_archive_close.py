"""The archive writer's close: bounded by its timeout; nothing is written after it."""

import time
from types import SimpleNamespace

import pytest

from togeari.plugins.record import archive
from togeari.plugins.record.archive import ArchiveWriter


@pytest.mark.serial
def test_close_returns_within_its_timeout_and_nothing_writes_after(tmp_path, monkeypatch):
    written: list[str] = []

    def slow_entry(record, inputs, run, program):
        time.sleep(0.05)
        return b"x", []

    monkeypatch.setattr(archive, "job_entry", slow_entry)
    writer = ArchiveWriter(tmp_path / "records")
    real_put = archive._Records.put
    monkeypatch.setattr(
        archive._Records,
        "put",
        lambda self, key, data: (written.append(key), real_put(self, key, data)),
    )

    # 200 slow records would take 10 s; close gives up after about twice 0.2 s
    for i in range(200):
        writer.add_job(SimpleNamespace(key=f"k{i}"), None, None)
    t0 = time.monotonic()
    writer.close(timeout=0.2)
    assert time.monotonic() - t0 < 3.0
    assert writer.records is None

    # Whatever the thread still had queued writes nothing once closed
    count = len(written)
    time.sleep(0.3)
    assert len(written) == count < 200


@pytest.mark.serial
def test_close_does_not_wait_for_a_slow_open_and_the_writer_closes_the_files(tmp_path, monkeypatch):
    """Opening the record files can take seconds on a cold disk: close returns by its deadline,
    the open's task writes nothing, and the writer closes what it opened."""
    opened: list = []
    real_init = archive._Records.__init__

    def slow_init(self, folder, append):
        time.sleep(1.5)
        real_init(self, folder, append)
        opened.append(self)

    monkeypatch.setattr(archive._Records, "__init__", slow_init)
    monkeypatch.setattr(archive, "job_entry", lambda record, inputs, run, program: (b"x", []))
    writer = ArchiveWriter(tmp_path / "records")
    writer.add_job(SimpleNamespace(key="k0"), None, None)
    time.sleep(0.2)
    t0 = time.monotonic()
    writer.close(timeout=0.2)
    assert time.monotonic() - t0 < 1.0
    writer.worker.thread.join(5)
    assert len(opened) == 1 and writer.records is None and writer.written == 0
