"""The device process of ``TorchEngine``'s process isolation.

Messages from the engine (``requests``): ``("run", id, model, n, packet)``,
``("cancel", id)``, ``("release", id)`` once a reply's blocks were read, and
``("stop",)``. Messages to the engine (``replies``): ``("ready", footprint)`` or
``("failed", packed exception)`` once, then ``("done", id, kind, body, stats)``
per job with ``kind`` one of ``"ok"`` (body: packet of results), ``"fail"``
(body: packed exception) or ``"skip"``. The process is in the worker registry
(``engines.common.registry``) while it runs.
"""

import contextlib
import threading
from functools import partial
from typing import Any

from togeari.engines.common.registry import registered
from togeari.engines.common.transport import Packet, pack_error, release
from togeari.engines.torch.codec import tensor_codec
from togeari.engines.torch.runtime import DeviceRuntime, Job, Skipped


def given(payloads: list) -> list:
    return payloads


def nothing() -> list:
    return []


def main(requests: Any, replies: Any, device: str | tuple, models: dict, config: dict) -> None:
    with registered(__name__):
        serve(requests, replies, device, models, config)


def serve(requests: Any, replies: Any, device: str | tuple, models: dict, config: dict) -> None:
    codec = tensor_codec(shm=config["shm"], threshold=config["threshold"])
    try:
        runtime = DeviceRuntime(device, models, **config["runtime"])
        footprint = runtime.start()
    except Exception as exc:  # noqa: BLE001
        replies.send(("failed", pack_error(exc)))
        return
    replies.send(("ready", footprint))

    # Guarded by ``lock``: the reply pipe, jobs, result blocks held for the engine,
    # input blocks per job, and input blocks still exported by live tensors.
    lock = threading.Lock()
    jobs: dict[int, Job] = {}
    held: dict[int, list] = {}
    mapped: dict[int, list] = {}
    stale: list = []

    def reply(job: Job, outcome: Any) -> None:
        owned: list = []
        match outcome:
            case Skipped():
                kind, body = "skip", None
            case BaseException():
                kind, body = "fail", pack_error(outcome)
            case _:
                try:
                    body, owned = codec.encode(outcome)
                    kind = "ok"
                except Exception as exc:  # noqa: BLE001
                    kind, body = "fail", pack_error(exc)
        with lock:
            jobs.pop(job.id, None)
            if owned:
                held[job.id] = owned
            stale.extend(mapped.pop(job.id, []))
            stale[:] = release(stale, unlink=False)
            with contextlib.suppress(EOFError, OSError):
                replies.send(("done", job.id, kind, body, runtime.stats()))

    while True:
        try:
            msg = requests.recv()
        except (EOFError, OSError):
            break

        match msg:
            case ("run", int() as job_id, str() as model, int() as n, Packet() as packet):
                try:
                    payloads, opened = codec.decode(packet, copy=False)
                except Exception as exc:  # noqa: BLE001
                    reply(Job(job_id, model, n, nothing, reply), exc)
                    continue
                job = Job(job_id, model, n, partial(given, payloads), reply)
                del payloads
                with lock:
                    mapped[job_id] = opened
                    jobs[job_id] = job
                runtime.put(job)
            case ("cancel", int() as job_id):
                with lock:
                    job = jobs.get(job_id)
                if job is not None:
                    job.cancelled = True
            case ("release", int() as job_id):
                with lock:
                    release(held.pop(job_id, []), unlink=True)
            case ("stop",):
                break

    # Leaving: queued jobs are answered by the runtime, then every block is freed.
    runtime.stop()
    with lock:
        for blocks in held.values():
            release(blocks, unlink=True)
        held.clear()
