"""Placements: each placement its own model object, the default placement, no runtime for an
unused device, and the caller thread's CUDA device left alone."""

import threading

import pytest

torch = pytest.importorskip("torch")

import tiny_models  # noqa: E402
from test_torch_split import deep, reference, run_one  # noqa: E402
from torch.types import Device  # noqa: E402

from togeari.core.contracts.results import Ok  # noqa: E402
from togeari.engines.torch import TorchEngine  # noqa: E402

needs_cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs CUDA")


def first_weight(unit) -> torch.Tensor:
    return next(unit.model.module.parameters())


# ---------------------------------------------------------------- one object per placement
def test_two_placements_in_one_process_load_independent_objects():
    model = deep(stages=[0, 0, 1, 1, 1])
    e = TorchEngine(devices=["cpu"], dtype="float32", isolation="thread")
    e.add_model("deep", model, devices=["cpu", ("cpu", "cpu")])
    alone, split = e.unit("deep", "cpu"), e.unit("deep", ("cpu", "cpu"))
    items = tiny_models.items("deep", n=4)
    want = reference(model, items)

    alone.start()
    split.start()
    try:
        # Distinct objects: the first placement loads the registered one, unsplit
        assert alone.model is model and split.model is not model
        assert alone.model.parts is None and split.model.parts is not None

        # Both answer the unsplit outputs
        for unit in (alone, split):
            results = run_one(unit, items)
            assert all(isinstance(r, Ok) for r in results), results
            for r, w in zip(results, want):
                assert torch.allclose(r.value.float(), w, atol=1e-5)

        # A change to one placement's weights does not show in the other's
        before = first_weight(split).detach().clone()
        with torch.no_grad():
            first_weight(alone).add_(1.0)
        assert torch.equal(first_weight(split), before)
    finally:
        alone.stop()
        split.stop()


def test_process_isolation_hands_every_placement_the_registered_object():
    model = deep()
    e = TorchEngine(devices=["cpu"], dtype="float32", isolation="process")
    e.add_model("deep", model, devices=["cpu", ("cpu", "cpu")])

    # Each process unpickles its own copy, so no copy is made here
    assert e.unit("deep", "cpu").model is model
    assert e.unit("deep", ("cpu", "cpu")).model is model
    assert e.models["deep"].copies == {}


# ---------------------------------------------------------------- default placement, idle devices
def test_a_model_without_devices_is_on_the_first_device_and_idle_devices_start_nothing():
    e = TorchEngine(devices=["cpu", "cuda:0"], dtype="float32")
    e.add_model("deep", deep())
    assert [u.device for u in e.units("deep")] == ["cpu"]

    # cuda:0 holds no model: no runtime for it
    assert e.placements() == [("cpu",)]


# ---------------------------------------------------------------- the caller's CUDA device
@needs_cuda
def test_start_leaves_the_calling_threads_cuda_device_alone(monkeypatch):
    calls: list[int] = []
    original = torch.cuda.set_device

    def recording(device: Device) -> None:
        calls.append(threading.get_ident())
        original(device)

    monkeypatch.setattr(torch.cuda, "set_device", recording)
    before = torch.cuda.current_device()
    e = TorchEngine(devices=["cuda:0"], memory_cap_gb=1.0, dtype="float32")
    e.add_model("deep", deep(), devices=["cuda:0"])
    unit = e.unit("deep")
    unit.start()
    try:
        # Only the placement's own worker threads select the device
        assert threading.get_ident() not in calls
        assert torch.cuda.current_device() == before
        results = run_one(unit, tiny_models.items("deep", n=2))
        assert all(isinstance(r, Ok) for r in results), results
    finally:
        unit.stop()
