"""The tensor digest registered by ``togeari.engines.torch``, against an independent byte
reference."""

import hashlib

import pytest

torch = pytest.importorskip("torch")

import togeari.engines.torch  # noqa: E402, F401
from togeari.core.contracts.digest import digest, digest_with_weak  # noqa: E402

DTYPES = [torch.float32, torch.bfloat16, torch.float16, torch.int64, torch.uint8, torch.bool]


def reference(t):
    """One registered piece: tag ``r``, its length, then header plus the bytes of a fresh
    contiguous copy's storage."""
    data = f"tensor{tuple(t.shape)}{t.dtype}".encode()
    data += bytes(t.contiguous().clone().untyped_storage())
    return hashlib.sha256(b"r" + len(data).to_bytes(8, "little") + data).digest()


@pytest.mark.parametrize("dtype", DTYPES, ids=str)
def test_tensor_digest_is_its_raw_bytes(dtype):
    t = torch.arange(12).reshape(3, 4).to(dtype)
    assert digest(t) == reference(t)
    assert digest_with_weak([t])[1] == []


def test_views_and_devices_digest_by_value():
    big = torch.arange(24, dtype=torch.bfloat16).reshape(4, 6)
    view = big[1:3, ::2]
    assert digest(view) == reference(view)
    assert digest(big[1:3]) != digest(big[2:4])
    assert digest(big.view(torch.int16)) != digest(big)
    assert digest(big.reshape(6, 4)) != digest(big)


@pytest.mark.gpu
@pytest.mark.skipif(not torch.cuda.is_available(), reason="needs CUDA")
def test_a_device_tensor_digests_as_its_host_copy():
    big = torch.arange(24, dtype=torch.bfloat16).reshape(4, 6)
    assert digest(big.to("cuda:0")) == digest(big)
