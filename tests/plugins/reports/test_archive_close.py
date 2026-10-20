"""The archive writer's close: bounded by its timeout; nothing is written after it."""

import time
from types import SimpleNamespace

from togeari.plugins.record import archive
from togeari.plugins.record.archive import ArchiveWriter


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
