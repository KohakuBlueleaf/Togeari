"""Digests: injective over containers of strings and bytes, and arrays by dtype, shape and bytes."""

import hashlib

import pytest

from togeari.core.contracts.digest import digest


@pytest.mark.parametrize(
    "a, b",
    [
        (("a;sb", ""), ("a", "b;s")),
        ((b"x;b", b""), (b"x", b";b")),
        (["ab", "c"], ["a", "bc"]),
        ({"k": "v;s"}, {"k;s": "v"}),
        (("1", 1), (1, "1")),
        ((None,), ("None",)),
    ],
)
def test_different_values_have_different_digests(a, b):
    assert digest(a) != digest(b)


def test_equal_values_have_equal_digests():
    assert digest({"b": [1, "x"], "a": b"y"}) == digest({"a": b"y", "b": [1, "x"]})


def test_arrays_digest_by_dtype_shape_and_bytes():
    np = pytest.importorskip("numpy")
    a = np.arange(12, dtype=np.int32).reshape(3, 4)

    # An independent reference: two framed pieces, the header then the bytes
    header = f"{tuple(a.shape)}{a.dtype}".encode()
    body = a.tobytes()
    ref = hashlib.sha256(
        b"a"
        + len(header).to_bytes(8, "little")
        + header
        + b"A"
        + len(body).to_bytes(8, "little")
        + body
    ).digest()
    assert digest(a) == ref

    # Same bytes in another shape or dtype differ
    assert digest(a.reshape(4, 3)) != digest(a)
    assert digest(a.view(np.uint32)) != digest(a)
