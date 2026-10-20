"""TorchEngine start and stop: a start failing partway stops what it started; a stop keeps the
weights of a forward that still runs."""

import pytest

torch = pytest.importorskip("torch")

import tiny_models  # noqa: E402
from torch import nn  # noqa: E402

from togeari.engines.torch import TorchEngine, TorchModule  # noqa: E402


class Recorder(TorchModule):
    """A linear model that records every ``to`` call."""

    def __init__(self):
        torch.manual_seed(0)
        super().__init__(nn.Linear(tiny_models.FEATURES, 8), max_batch=8, name="recorder")
        self.moves: list = []

    def to(self, device):
        self.moves.append(device)
        super().to(device)


class Broken(TorchModule):
    """A linear model whose ``load`` raises."""

    def __init__(self):
        super().__init__(nn.Linear(tiny_models.FEATURES, 8), max_batch=8, name="broken")

    def load(self, device, dtype):
        raise RuntimeError("load failed")


def test_a_start_that_fails_partway_stops_the_device_it_started():
    engine = TorchEngine(devices=["cpu"], isolation="thread", schedule="co_resident")
    recorder = Recorder()
    engine.add_model("first", recorder)
    engine.add_model("second", Broken())
    with pytest.raises(RuntimeError, match="load failed"):
        engine.start()

    # The device that loaded "first" was stopped: its weights were moved off it
    assert "cpu" in recorder.moves


def test_a_stop_keeps_the_weights_of_a_forward_that_still_runs(monkeypatch):
    engine = TorchEngine(devices=["cpu"], isolation="thread", schedule="co_resident")
    recorder = Recorder()
    engine.add_model("first", recorder)
    engine.start()
    (backend,) = engine.backends.values()
    runtime = backend.runtime

    # A worker that has not ended by the join: the weights stay where they are
    class Alive:
        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

    real = list(runtime.workers)
    runtime.workers = [*real, Alive()]
    moves = len(recorder.moves)
    engine.stop()
    assert len(recorder.moves) == moves
    for worker in real:
        worker.join(5.0)
