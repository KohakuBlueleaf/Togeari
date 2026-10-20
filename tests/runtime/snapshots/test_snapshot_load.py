"""Reading a snapshot from a store: hits, misses and a failing store."""

import pytest

from togeari.core.contracts.io import MISS
from togeari.core.runtime.retire import snapshot


class Store:
    def __init__(self, value=MISS, error: BaseException | None = None):
        self.value = value
        self.error = error

    def get(self, key):
        if self.error is not None:
            raise self.error
        return self.value


def test_a_stored_blob_is_returned_and_a_miss_is_none():
    assert snapshot.load(Store(b"state"), b"k") == b"state"
    assert snapshot.load(Store(MISS), b"k") is None
    assert snapshot.load(Store(None), b"k") is None
    assert snapshot.load(None, b"k") is None


def test_a_failing_store_read_is_reported_and_reads_as_no_snapshot():
    seen = []
    err = OSError("disk gone")
    assert snapshot.load(Store(error=err), b"k", on_error=seen.append) is None
    assert seen == [err]

    # Without a handler the error is logged, not raised
    assert snapshot.load(Store(error=err), b"k") is None


def test_an_interrupt_from_the_store_is_not_swallowed():
    with pytest.raises(KeyboardInterrupt):
        snapshot.load(Store(error=KeyboardInterrupt()), b"k")
