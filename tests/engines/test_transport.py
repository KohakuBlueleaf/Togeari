"""Shared-memory transport: an independent process (its own resource tracker, as an engine
host is) that attaches to a block another process owns leaves it alone when it exits (before
Python 3.13 its tracker would unlink the owner's live block); a packet's buffers round-trip
through blocks."""

import subprocess
import sys

import numpy as np

from togeari.engines.common.transport import Codec, release

READ = (
    "import sys; from togeari.engines.common.transport import attach; "
    "b = attach(sys.argv[1]); sys.stdout.buffer.write(bytes(b.buf[: int(sys.argv[2])])); "
    "b.close()"
)


def test_an_attached_block_outlives_the_reader():
    codec = Codec(shm=True, threshold=16)
    payload = np.arange(4096, dtype=np.uint8)
    packet, owned = codec.encode(payload)
    assert len(packet.blocks) == 1
    name, n = packet.blocks[0]
    try:
        run = subprocess.run(
            [sys.executable, "-c", READ, name, str(n)], capture_output=True, timeout=120
        )
        assert run.returncode == 0, run.stderr.decode()
        assert run.stdout == payload.tobytes()
        assert b"resource_tracker" not in run.stderr, run.stderr.decode()
        # The reader's exit did not unlink the owner's block
        got, mapped = codec.decode(packet, copy=True)
        assert mapped == [] and np.array_equal(got, payload)
    finally:
        release(owned, unlink=True)
