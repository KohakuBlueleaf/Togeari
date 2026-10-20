"""Tensors through the process transport: raw bytes out of band, rebuilt with ``frombuffer``."""

import ctypes
import pickle
from typing import Any

import torch

from togeari.engines.common.transport import SHM_THRESHOLD, Codec


def reduce_tensor(t: torch.Tensor, keep: list) -> Any:
    """A strided tensor as (dtype, shape, its bytes); device tensors are copied to the host."""
    if t.layout != torch.strided or t.is_quantized:
        return NotImplemented
    t = t.detach().resolve_conj().resolve_neg()
    if t.device.type != "cpu":
        t = t.cpu()
    t = t.contiguous()
    dtype = str(t.dtype).removeprefix("torch.")
    nbytes = t.numel() * t.element_size()
    if nbytes == 0:
        return empty_tensor, (dtype, tuple(t.shape))

    # A byte view of the tensor's memory; ``keep`` pins the tensor until the packet is built.
    keep.append(t)
    raw = (ctypes.c_uint8 * nbytes).from_address(t.data_ptr())
    return rebuild_tensor, (pickle.PickleBuffer(raw), dtype, tuple(t.shape))


def rebuild_tensor(buf: Any, dtype: str, shape: tuple) -> torch.Tensor:
    return torch.frombuffer(buf, dtype=torch.uint8).view(getattr(torch, dtype)).reshape(shape)


def empty_tensor(dtype: str, shape: tuple) -> torch.Tensor:
    return torch.empty(shape, dtype=getattr(torch, dtype))


def tensor_codec(shm: bool, threshold: int = SHM_THRESHOLD) -> Codec:
    return Codec(shm=shm, threshold=threshold, reducers={torch.Tensor: reduce_tensor})
