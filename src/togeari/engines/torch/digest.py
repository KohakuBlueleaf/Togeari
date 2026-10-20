"""Tensor digests: a header with shape and dtype, then the raw bytes through a uint8 view.

The uint8 view covers every dtype, including those numpy lacks (bfloat16).
``togeari.engines.torch`` registers ``tensor_digest`` for ``torch.Tensor``.
"""

import torch


def tensor_digest(t: torch.Tensor) -> bytes:
    arr = t.detach().resolve_conj().resolve_neg().cpu().contiguous()
    header = f"tensor{tuple(arr.shape)}{arr.dtype}".encode()
    return header + arr.reshape(-1).view(torch.uint8).numpy().tobytes()
