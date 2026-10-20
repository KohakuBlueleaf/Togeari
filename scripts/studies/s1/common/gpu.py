"""The one CUDA device of the study (``cuda:0``): allocator cap and memory counters."""

import torch

DEVICE = "cuda:0"


def guard(cap_gb: float) -> dict:
    """Cap this process's allocator on ``cuda:0`` before anything is allocated there."""
    if not torch.cuda.is_available():
        raise RuntimeError("no CUDA device")
    total = torch.cuda.get_device_properties(DEVICE).total_memory
    fraction = min(1.0, cap_gb * 2**30 / total)
    torch.cuda.set_per_process_memory_fraction(fraction, DEVICE)
    torch.cuda.reset_peak_memory_stats(DEVICE)
    return {
        "device": DEVICE,
        "name": torch.cuda.get_device_name(DEVICE),
        "total_mb": total / 2**20,
        "cap_gb": cap_gb,
        "fraction": fraction,
    }


def memory() -> dict | None:
    """Allocator counters of this process on ``cuda:0`` (MiB); None when CUDA is not in use here."""
    if not torch.cuda.is_initialized():
        return None
    return {
        "allocated_mb": torch.cuda.memory_allocated(DEVICE) / 2**20,
        "reserved_mb": torch.cuda.memory_reserved(DEVICE) / 2**20,
        "max_allocated_mb": torch.cuda.max_memory_allocated(DEVICE) / 2**20,
        "max_reserved_mb": torch.cuda.max_memory_reserved(DEVICE) / 2**20,
    }


def reset_peak() -> None:
    if torch.cuda.is_initialized():
        torch.cuda.reset_peak_memory_stats(DEVICE)
