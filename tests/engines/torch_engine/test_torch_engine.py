"""TorchEngine on cuda:0 with tiny models: behaviour checks, swapping, footprint, process death."""

import copy
import os
import threading
import time

import pytest

torch = pytest.importorskip("torch")

import tiny_models  # noqa: E402

from togeari.adapters.torch import TorchAdapter  # noqa: E402
from togeari.core.contracts.results import Cancelled, ItemError, Ok, UnitError  # noqa: E402
from togeari.core.contracts.workitem import WorkItem  # noqa: E402
from togeari.engines.common.errors import WorkerDied  # noqa: E402
from togeari.engines.common.state import EngineState  # noqa: E402
from togeari.engines.torch import TorchEngine  # noqa: E402
from togeari.testing import check_engine  # noqa: E402

DEVICE = "cuda:0"
CAP_GB = 2.0


def on_gpu(fn):
    """A test on cuda:0: marked ``gpu``, skipped without CUDA."""
    needs = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs CUDA")
    return pytest.mark.gpu(needs(fn))


def engine(**knobs):
    return TorchEngine(devices=[DEVICE], memory_cap_gb=CAP_GB, **knobs)


def run_one(unit, items, timeout=60.0):
    got = []
    event = threading.Event()

    def done(ticket, results):
        got.extend(results)
        event.set()

    unit.submit(items, done)
    assert event.wait(timeout), "no answer"
    return got


def until(cond, timeout: float):
    """Wait for ``cond()``; its last value."""
    deadline = time.monotonic() + timeout
    while not (value := cond()) and time.monotonic() < deadline:
        time.sleep(0.02)
    return value


def expected(model, item):
    """The model's output for one item, in float32 on the CPU."""
    ref = copy.deepcopy(model.module).float().cpu()
    with torch.inference_mode():
        return ref(item.inputs["x"][None].float())[0]


@pytest.mark.parametrize("isolation", ["thread", "process"])
@pytest.mark.parametrize("schedule", ["co_resident", "swappable", "priority_streams"])
@on_gpu
def test_torch_unit_keeps_every_rule(isolation, schedule):
    if isolation == "process" and schedule == "priority_streams":
        pytest.skip("stream scheduling is the same code in both isolations")

    e = engine(schedule=schedule, isolation=isolation)
    e.add_model("lin", tiny_models.linear(0))
    unit = e.unit("lin")

    # Process isolation: the device process is terminated; thread isolation has none to kill.
    def kill():
        if isolation == "process":
            e.backends[DEVICE].proc.kill()
        else:
            e.stop()

    report = check_engine(
        lambda: unit,
        tiny_models.items("lin"),
        failing_items=[tiny_models.bad_item("lin")],
        kill=kill,
        tolerance=0.05,
        rounds=60,
        timeout_s=60.0,
    )
    assert report.ok, str(report)


@pytest.mark.parametrize("isolation", ["thread", "process"])
@on_gpu
def test_swappable_alternates_models_with_correct_outputs(isolation):
    e = engine(schedule="swappable", isolation=isolation, swap_quantum_ms=1.0)
    models = {"a": tiny_models.linear(1), "b": tiny_models.linear(2)}
    for name, model in models.items():
        e.add_model(name, copy.deepcopy(model))
    units = {name: e.unit(name) for name in models}
    for unit in units.values():
        unit.start()

    # Three rounds alternating the two models
    try:
        for round_ in range(3):
            for name, unit in units.items():
                batch = tiny_models.items(name, n=4, seed=round_)
                results = run_one(unit, batch)
                for item, r in zip(batch, results):
                    assert isinstance(r, Ok), r
                    want = expected(models[name], item)
                    assert torch.allclose(r.value.float().cpu(), want, atol=0.05, rtol=0.05)
        stats = e.stats()[DEVICE]
        footprint = units["a"].load().extra["footprint"]
    finally:
        for unit in units.values():
            unit.stop()

    # Every round swapped; the footprint and the stopped engine
    assert stats["swaps"] >= 6 and stats["swap_s"] > 0.0, stats
    assert stats["models"]["a"]["batches"] == 3 and stats["models"]["b"]["items"] == 12
    assert footprint["weights_mb"] > 0.0 and footprint["peak_mb"] >= footprint["weights_mb"]
    assert e.ready() is False


@on_gpu
def test_memory_cap_is_set_before_weights_load():
    torch.cuda.set_per_process_memory_fraction(1.0, 0)
    probe = tiny_models.CapProbe()
    e = engine()
    e.add_model("probe", probe)
    e.start()
    try:
        stats = e.stats()
    finally:
        e.stop()

    # The fraction the model's load saw, before any weight was on the device
    total = torch.cuda.get_device_properties(0).total_memory
    assert probe.fraction_at_load is not None
    assert abs(probe.fraction_at_load - CAP_GB * 2**30 / total) < 1e-6
    assert stats[DEVICE]["footprint"]["probe"]["weights_mb"] > 0.0


@on_gpu
def test_cancel_of_a_hung_batch_kills_the_device_process_and_capacity_returns():
    e = engine(isolation="process", slots=1, cancel_kill_s=0.5)
    e.add_model("hang", tiny_models.hang())
    unit = e.unit("hang")
    unit.start()
    try:
        backend = e.backends[DEVICE]
        got = []
        ticket = unit.submit(tiny_models.hang_items(), lambda t, r: got.extend(r))
        assert until(lambda: backend.jobs, 30.0), "the batch never reached the device"
        assert unit.capacity() == 0

        # The cancel answers at once; the device process is killed and restarted
        unit.cancel(ticket)
        assert got == [Cancelled(), Cancelled()]
        assert until(lambda: unit.capacity() == 1, 120.0), "capacity never came back"
        assert backend.kills == 1 and backend.restarts == 1

        # The restarted device runs batches again
        batch = tiny_models.items("hang", n=2)
        results = run_one(unit, batch)
        for item, r in zip(batch, results):
            assert torch.allclose(r.value.float(), item.inputs["x"] * 2, atol=0.05, rtol=0.02)
        assert unit.load().extra["kills"] == 1
    finally:
        unit.stop()


@on_gpu
def test_a_lost_device_revives_through_ready(tmp_path):
    flag = tmp_path / "fail-load"
    e = engine(isolation="process", revive_backoff_s=(0.2, 0.4))
    e.add_model("flaky", tiny_models.Flaky(str(flag)))
    unit = e.unit("flaky")
    unit.start()
    try:
        backend = e.backends[DEVICE]

        # The process dies and cannot restart: the device is lost
        flag.write_text("x")
        backend.proc.kill()
        assert until(lambda: backend.lost, 120.0)
        assert not unit.ready() and not e.ready()

        # Loads work again: ready() revives the device after its backoff
        flag.unlink()
        assert until(unit.ready, 120.0), "the lost device never revived"
        assert not backend.lost and e.ready()
        results = run_one(unit, tiny_models.items("flaky", n=2))
        assert all(isinstance(r, Ok) for r in results), results
    finally:
        unit.stop()


def test_starts_wait_for_the_start_in_progress():
    gated = tiny_models.Gated()
    e = TorchEngine(devices=["cpu"], dtype="float32")
    e.add_model("gated", gated)
    e.add_model("lin", tiny_models.linear(0))
    first, second = e.unit("gated"), e.unit("lin")
    seen = {}

    def start_second():
        second.start()
        seen["ready"] = e.ready()

    t1 = threading.Thread(target=first.start)
    t1.start()
    assert gated.entered.wait(30.0)
    t2 = threading.Thread(target=start_second)
    t2.start()
    t2.join(0.3)
    assert t2.is_alive(), "a second start returned while the first was loading"
    gated.gate.set()
    t1.join(30.0)
    t2.join(30.0)
    try:
        assert seen == {"ready": True}
        assert e.started_units == {first.name, second.name}
    finally:
        first.stop()
        second.stop()
    assert not e.ready()


def test_stop_during_start_stops_once_the_start_ends():
    gated = tiny_models.Gated()
    e = TorchEngine(devices=["cpu"], dtype="float32")
    e.add_model("gated", gated)
    starter = threading.Thread(target=e.start)
    starter.start()
    assert gated.entered.wait(30.0)
    stopper = threading.Thread(target=e.stop)
    stopper.start()
    stopper.join(0.3)
    assert stopper.is_alive(), "stop returned while the start was loading"
    gated.gate.set()
    starter.join(30.0)
    stopper.join(30.0)
    assert e.state == EngineState.STOPPED and e.backends == {} and not e.ready()


def test_a_failed_start_fails_every_waiting_starter():
    gated = tiny_models.Gated()
    gated.error = RuntimeError("load failed")
    e = TorchEngine(devices=["cpu"], dtype="float32")
    e.add_model("gated", gated)
    errors = []

    def start():
        try:
            e.start()
        except RuntimeError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    threads[0].start()
    assert gated.entered.wait(30.0)
    threads[1].start()
    gated.gate.set()
    for t in threads:
        t.join(30.0)
    assert [str(x) for x in errors] == ["load failed", "load failed"]
    assert e.state == EngineState.STOPPED


@pytest.mark.parametrize("isolation", ["thread", "process"])
@on_gpu
def test_spec_is_identity_only(isolation):
    e = engine(isolation=isolation)
    e.add_model("lin", tiny_models.linear(0))
    unit = e.unit("lin")
    before = (e.spec(), unit.spec())
    unit.start()
    try:
        after = (e.spec(), unit.spec())
        footprint = unit.load().extra["footprint"]
    finally:
        unit.stop()
    assert after == before
    assert footprint["weights_mb"] > 0.0


@on_gpu
def test_process_death_fails_the_running_batch_then_restarts():
    e = engine(isolation="process")
    e.add_model("sleepy", tiny_models.sleepy())
    unit = e.unit("sleepy")
    unit.start()
    try:
        backend = e.backends[DEVICE]
        got = []
        event = threading.Event()
        unit.submit(tiny_models.items("sleepy", n=2), lambda t, r: (got.extend(r), event.set()))
        assert until(lambda: backend.jobs, 30.0), "the batch never reached the device"
        os.kill(backend.proc.pid, 9)
        assert event.wait(60.0)
        assert all(isinstance(r, ItemError) and isinstance(r.error, WorkerDied) for r in got), got

        # While the process restarts the unit is down: a batch fails at once with UnitError.
        assert not unit.ready() and unit.capacity() == 0
        t0 = time.monotonic()
        results = run_one(unit, tiny_models.items("sleepy", n=2), timeout=2.0)
        assert all(isinstance(r, UnitError) for r in results), results
        assert time.monotonic() - t0 < 2.0

        # The device process restarts with its models; the next batch runs.
        deadline = time.monotonic() + 60.0
        while backend.restarts == 0 or not backend.alive:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        batch = tiny_models.items("sleepy", n=2)
        results = run_one(unit, batch)
        for item, r in zip(batch, results):
            assert torch.allclose(r.value.float(), item.inputs["x"] * 2, atol=0.05, rtol=0.02)
        assert unit.load().extra["restarts"] == 1
    finally:
        unit.stop()


class Heavy(TorchAdapter):
    """A heavy adapter: ``prepare`` may run on engine threads only."""

    light = False

    def __init__(self, keyed: bool):
        super().__init__("x")
        self.keyed = keyed
        self.prepared_on: list[int] = []

    def prepare(self, inputs, protocol):
        self.prepared_on.append(threading.get_ident())
        return super().prepare(inputs, protocol)

    def __getattr__(self, name):
        if name == "key" and self.keyed:
            return lambda inputs, protocol: tuple(inputs["x"].shape)
        raise AttributeError(name)


@pytest.mark.parametrize("keyed", [True, False])
@on_gpu
def test_unit_key_never_prepares_a_heavy_payload(keyed):
    e = engine()
    e.add_model("lin", tiny_models.linear(0))
    unit = e.unit("lin")
    heavy = Heavy(keyed)
    items = [
        WorkItem("lin", {"x": torch.randn(tiny_models.FEATURES)}, heavy, "torch") for _ in range(2)
    ]
    keys = [unit.key(it) for it in items]
    assert heavy.prepared_on == []
    if keyed:
        assert keys[0] == keys[1] == (tiny_models.FEATURES,)
    else:
        assert keys[0] != keys[1]

    # A light adapter's key is the model's key of the payload.
    light = tiny_models.items("lin", n=1)[0]
    assert unit.key(light) == ((tiny_models.FEATURES,), torch.float32)

    # Batches prepare the heavy payloads on the engine's threads.
    unit.start()
    try:
        results = run_one(unit, items)
    finally:
        unit.stop()
    assert all(isinstance(r, Ok) for r in results), results
    assert heavy.prepared_on and threading.get_ident() not in heavy.prepared_on


@on_gpu
def test_submit_after_stop_is_unit_error():
    e = engine()
    e.add_model("lin", tiny_models.linear(0))
    unit = e.unit("lin")
    unit.start()
    unit.stop()
    results = run_one(unit, tiny_models.items("lin", n=2))
    assert all(isinstance(r, UnitError) for r in results)
