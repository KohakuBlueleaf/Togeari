"""A small fixed-seed network that maps 16 features to 4 class scores."""

import torch
from spectra import BINS
from torch import nn

CLASSES = ["calm", "pulse", "drift", "burst"]


def make_module() -> nn.Module:
    gen = torch.Generator().manual_seed(1234)
    module = nn.Sequential(nn.Linear(BINS, 64), nn.GELU(), nn.Linear(64, len(CLASSES)))
    with torch.no_grad():
        for p in module.parameters():
            p.copy_(torch.randn(p.shape, generator=gen) * 0.5)
    return module


def collate(payloads: list) -> torch.Tensor:
    return torch.tensor(payloads, dtype=torch.float32)


def to_rows(out: torch.Tensor, n: int) -> list:
    return out.float().cpu().tolist()


def width(payload: list) -> int:
    return len(payload)
