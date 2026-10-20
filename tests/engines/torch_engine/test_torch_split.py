"""A model split across several devices: one unit, its stages on their devices, the unsplit
outputs."""

import copy
import threading

import pytest

torch = pytest.importorskip("torch")

import tiny_models  # noqa: E402
from torch import nn  # noqa: E402

from togeari.core.contracts.results import Ok  # noqa: E402
from togeari.engines.torch import TorchEngine, TorchModule  # noqa: E402
from togeari.engines.torch.model import stage_plan  # noqa: E402

FEATURES = tiny_models.FEATURES
needs_cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs CUDA")


def on_gpu(fn):
    """A test on cuda:0: marked ``gpu``, skipped without CUDA."""
    return pytest.mark.gpu(needs_cuda(fn))


def deep(seed: int = 0, stages: list[int] | None = None) -> TorchModule:
    """Five children: two wide linears, a narrow one, activations between."""
    torch.manual_seed(seed)
    module = nn.Sequential(
        nn.Linear(FEATURES, 64), nn.ReLU(), nn.Linear(64, 64), nn.ReLU(), nn.Linear(64, 8)
    )
    return TorchModule(
        module, max_batch=8, example=[torch.zeros(FEATURES)] * 2, name="deep", stages=stages
    )


def reference(model: TorchModule, items: list) -> list:
    """The unsplit module's output per item, in float32 on the CPU."""
    ref = copy.deepcopy(model.module).float().cpu()
    with torch.inference_mode():
        return [ref(item.inputs["x"][None].float())[0] for item in items]


def run_one(unit, items, timeout: float = 60.0) -> list:
    got, event = [], threading.Event()

    def done(ticket, results):
        got.extend(results)
        event.set()

    unit.submit(items, done)
    assert event.wait(timeout), "no answer"
    return got


def stage_devices(model: TorchModule) -> list[set[str]]:
    """The devices holding each stage's parameters."""
    return [{str(p.device) for p in part.parameters()} for _, part in model.parts]


# ---------------------------------------------------------------- the stage plan
def test_the_stage_plan_gives_contiguous_runs_of_near_equal_parameters():
    assert stage_plan([10, 0, 10, 0, 10, 0], 3) == [0, 0, 1, 1, 2, 2]
    assert stage_plan([90, 0, 5, 5], 2) == [0, 0, 1, 1]
    assert stage_plan([1, 1, 1, 1], 1) == [0, 0, 0, 0]
    assert stage_plan([0, 0, 0, 0], 2) == [0, 0, 1, 1]
    plan = stage_plan([3, 7, 1, 9, 2, 8], 4)
    assert plan == sorted(plan) and plan[0] == 0 and max(plan) <= 3


def test_explicit_stages_must_fit_the_children_and_devices():
    with pytest.raises(ValueError, match="do not fit"):
        deep(stages=[0, 1]).load(("cpu", "cpu"), torch.float32)
    with pytest.raises(ValueError, match="do not fit"):
        deep(stages=[0, 0, 1, 1, 2]).load(("cpu", "cpu"), torch.float32)
    with pytest.raises(TypeError, match="nn.Sequential"):
        TorchModule(nn.Linear(FEATURES, 8)).load(("cpu", "cpu"), torch.float32)


# ---------------------------------------------------------------- one unit per split
def test_a_split_model_is_one_unit_with_the_unsplit_outputs():
    model = deep(stages=[0, 0, 1, 1, 1])
    e = TorchEngine(devices=["cpu"], dtype="float32")
    e.add_model("deep", model, devices=[("cpu", "cpu")])

    # One unit for the tuple, named by it; no runtime for "cpu" alone
    assert [u.name for u in e.units("deep")] == ["torch:deep@cpu+cpu"]
    unit = e.unit("deep")
    assert unit is e.unit("deep", ("cpu", "cpu")) and unit.devices == ("cpu", "cpu")
    assert e.placements() == [("cpu", "cpu")]

    items = tiny_models.items("deep", n=6)
    unit.start()
    try:
        assert set(e.backends) == {"cpu+cpu"}
        assert [dev for dev, _ in model.parts] == ["cpu", "cpu"]
        assert len(model.parts) == 2
        results = run_one(unit, items)
    finally:
        unit.stop()
    assert all(isinstance(r, Ok) for r in results), results
    for r, want in zip(results, reference(model, items)):
        assert torch.allclose(r.value.float(), want, atol=1e-5)


def test_one_model_alone_and_split_gives_one_unit_per_placement():
    e = TorchEngine(devices=["cpu"], dtype="float32")
    e.add_model("deep", deep(), devices=["cpu", ("cpu", "cpu")])
    assert sorted(u.device for u in e.units("deep")) == ["cpu", "cpu+cpu"]
    assert e.placements() == [("cpu",), ("cpu", "cpu")]
    with pytest.raises(ValueError, match="name a device"):
        e.unit("deep")


def test_a_split_on_an_unknown_device_or_twice_is_refused():
    e = TorchEngine(devices=["cpu"], dtype="float32")
    with pytest.raises(ValueError, match="not in"):
        e.add_model("deep", deep(), devices=[("cpu", "cuda:7")])
    with pytest.raises(ValueError, match="placed twice"):
        e.add_model("deep", deep(), devices=[("cpu", "cpu"), ["cpu", "cpu"]])


def test_process_isolation_shares_each_device_cap_among_its_processes():
    e = TorchEngine(devices=["cuda:0"], memory_cap_gb=2.0, isolation="process")
    e.add_model("a", deep(0), devices=["cuda:0"])
    e.add_model("b", deep(1), devices=[("cuda:0", "cuda:0")])
    places = e.placements()
    assert places == [("cuda:0",), ("cuda:0", "cuda:0")]
    assert e._caps(places) == [{"cuda:0": 1.0}, {"cuda:0": 1.0}]

    # Threads share one process: the cap is the engine's
    t = TorchEngine(devices=["cuda:0"], memory_cap_gb=2.0, isolation="thread")
    assert t._caps(places) == [2.0, 2.0]


def test_a_split_model_runs_in_one_device_process():
    model = deep(stages=[0, 0, 1, 1, 1])
    e = TorchEngine(devices=["cpu"], dtype="float32", isolation="process")
    e.add_model("deep", model, devices=[("cpu", "cpu")])
    unit = e.unit("deep")
    items = tiny_models.items("deep", n=4)
    unit.start()
    try:
        assert set(e.backends) == {"cpu+cpu"}
        results = run_one(unit, items)
    finally:
        unit.stop()
    assert all(isinstance(r, Ok) for r in results), results
    for r, want in zip(results, reference(model, items)):
        assert torch.allclose(r.value.float(), want, atol=1e-5)


@on_gpu
@pytest.mark.parametrize("schedule", ["priority_streams", "swappable"])
def test_a_split_across_the_device_and_the_host_places_each_stage(schedule):
    model = deep(stages=[0, 0, 1, 1, 1])
    e = TorchEngine(
        devices=["cuda:0", "cpu"], memory_cap_gb=1.0, dtype="float32", schedule=schedule
    )
    e.add_model("deep", model, devices=[("cuda:0", "cpu")])
    unit = e.unit("deep")
    items = tiny_models.items("deep", n=6)
    unit.start()
    try:
        # The cap is set on the CUDA device of the placement
        assert torch.cuda.get_per_process_memory_fraction(0) < 1.0
        results = run_one(unit, items)
        assert stage_devices(model) == [{"cuda:0"}, {"cpu"}]
        assert unit.load().extra["footprint"]["weights_mb"] is not None
    finally:
        unit.stop()
    assert all(isinstance(r, Ok) for r in results), results
    for r, want in zip(results, reference(model, items)):
        assert torch.allclose(r.value.float().cpu(), want, atol=1e-4)
