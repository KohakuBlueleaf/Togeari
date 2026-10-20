# Builtin engines

Three engines ship with Togeari. Each keeps the engine contract
([writing an engine](writing-an-engine.md)) and passes `check_engine`. Each code block on this
page runs on its own.

| Engine | Kind | Runs | Package (extra) |
|---|---|---|---|
| `PythonCallEngine` | call | node functions on worker threads or worker processes | `togeari.engines.python` |
| `WebCallEngine` | call | requests to any HTTP service | `togeari.engines.web` (`[web]`) |
| `TorchEngine` | batch, one unit per module and device | batched modules on devices under one scheduler | `togeari.engines.torch` (`[torch]`) |

## PythonCallEngine

```python
from kohakunode.testing import LIB

from togeari import Lane, Machine, Use
from togeari.engines.python import PythonCallEngine
from togeari.io import Iterable, Memory


def main():
    workers = PythonCallEngine(workers=2, mode="process", imports=("kohakunode.testing",))
    cpu = Lane([workers], name="cpu")
    machine = Machine(
        program="@def (a: int)main(b):\n    (a)inc(b)\n",
        entry="main",
        libraries=[LIB],
        bind={"inc": Use(cpu)},
    )
    with machine:
        sink = Memory()
        machine.run(Iterable([{"a": i} for i in range(8)]), sink)
        print(sorted(r.outputs["b"] for r in sink.records))
        print(workers.load().extra)


# Worker processes are spawned: the script's top level must not build a machine
if __name__ == "__main__":
    main()
```

| Argument | Default | Meaning |
|---|---|---|
| `workers` | 8 | slots; `capacity()` is the free ones |
| `mode` | `"thread"` | `"thread"`: worker threads in the machine's process (`shares_memory=True`); `"process"`: one spawned worker process per slot (`shares_memory=False`) |
| `transport` | `"shm"` | process mode: buffers of at least `shm_threshold` bytes cross in shared memory, the rest by pickle through a pipe; `"pickle"`: everything through the pipe |
| `shm_threshold` | 1 MiB (`SHM_THRESHOLD`) | |
| `imports` | `()` | modules every worker process imports once at start, before it reports ready (the node libraries) |
| `start_timeout_s`, `stop_timeout_s` | 60, 5 | a worker's ready wait; joining a slot at stop |
| `name` | `"python"` | |

In process mode, node functions cross by reference (module and name): they must be importable
top-level functions, and a script that builds a machine needs the `__main__` guard.

| Event | Result |
|---|---|
| the function raises | `ItemError` |
| a worker process dies under an item | `ItemError(WorkerDied)`; the process restarts |
| a worker process exits while idle | it leaves the engine and is replaced in the background |
| no worker left | the unit is down (`UnitError` for waiting and new items) until `ready()` brings replacements up |
| `cancel` of a waiting item | `Cancelled()`; it leaves the queue |
| `cancel` of a running item, process mode | `Cancelled()`; the worker process is killed and restarted |
| `cancel` of a running item, thread mode | `Cancelled()`; the function runs to its end (a thread cannot be stopped) and its slot stays busy until then |

The engine wakes its lanes (`add_wake`) when a slot frees without an answer: a restarted worker
is back, a cancelled item's slot is free, replacements came up. `load().extra` reports live
slots, state and counters (`items`, `ok`, `errors`, `cancelled`, `restarts`, `kills`,
`shm_bytes`).

## WebCallEngine

```python
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kohakunode import NodeLibrary

from togeari import Lane, Machine, Use
from togeari.adapters import JsonHttp
from togeari.engines.web import WebCallEngine
from togeari.io import Iterable, Memory


class Double(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = json.dumps({"y": body["x"] * 2}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Double)
threading.Thread(target=server.serve_forever, daemon=True).start()

lib = NodeLibrary("web")


@lib.node(outputs=["y"])
def double(x: int) -> int: ...


service = WebCallEngine(
    f"http://127.0.0.1:{server.server_address[1]}", max_inflight=8, rate=(200.0, 10.0)
)
api = Lane([service], name="api")
machine = Machine(
    program="@def (x: int)main(y):\n    (x)double(y)\n",
    entry="main",
    libraries=[lib],
    bind={"double": Use(api, adapter=JsonHttp("/double", build=dict, parse=lambda r: r["y"]))},
)
with machine:
    sink = Memory()
    machine.run(Iterable([{"x": i} for i in range(20)]), sink)
    print(sorted(r.outputs["y"] for r in sink.records))
    print(service.load().extra)
server.shutdown()
```

| Argument | Default | Meaning |
|---|---|---|
| `base_url` | — | the service |
| `max_inflight` | 64 | requests in flight; `capacity()` is the free part |
| `rate` | `None` | token bucket `(requests per second, burst)`, or a number (burst 1); `capacity()` is 0 while it is empty |
| `pool_size` | 2 | connections per client; `max_inflight` is split over `ceil(max_inflight / pool_size)` clients |
| `headers` | `{}` | sent with every request |
| `timeout_s` | 600 | per request |
| `connect_timeout_s` | 10 | opening a connection; past it the request is a `UnitError` (`ConnectTimeout`) |
| `health_path`, `health_timeout_s` | `None`, 2 | after a `UnitError`, `ready()` stays false until a GET of this path succeeds (sent on its own connection) |
| `max_retry_after_s` | 3600 | the longest block a `Retry-After` sets |
| `classify` | `default_classify` | outcome (response or exception) → `ItemError`, `UnitError` or `None` (decode it) |
| `load_probe`, `probe_interval_s` | `None`, 1 | a function polled in the background; its dict appears in `load().extra["probe"]` |
| `on_unit_error` | `"block"` | the other requests in flight after a `UnitError`: `"block"` lets them finish; `"cancel"` answers them with the same error at once |
| `name` | `"web"` | |

`default_classify` (`togeari.engines.web`): status 429 or 503 → `UnitError` with the
`Retry-After` delay (seconds or an HTTP date); another status ≥ 400 → `ItemError`; a refused or
timed-out connection → `UnitError`; any other exception → `ItemError`. A `UnitError` blocks the
unit: `ready()` is false and `capacity()` is 0 until the `Retry-After` delay has passed (and the
health GET succeeds, with `health_path`), so the lane sends it no new work. Only the request that
saw the error gets the `UnitError`. With `on_unit_error="block"` (the default), the other
requests in flight finish and answer normally. With `"cancel"`, each of them is answered with
the same error at once (`load().extra["unit_error_items"]` counts them). A running request is
never aborted, by a cancel or a unit error: it runs to its end, its late answer is dropped, and
its slot frees then (the engine wakes its lanes, so queued work takes the slot). A cancelled
request not yet sent is never sent.

`spec()` (the engine's identity in content-store keys) holds the base URL, `max_inflight`,
`rate` and `timeout_s`; `headers`, `pool_size`, `connect_timeout_s`, `on_unit_error` and the
other arguments are not part of it.

`wake_at()` tells the lane when the token bucket next allows a request or a `Retry-After`
block ends, so a rate-limited unit is pumped exactly when it can take work.

Adapters (`togeari.adapters`): `JsonHttp(path, build=..., parse=..., method="POST",
headers=None)` sends `build(inputs)` as the JSON body and returns `parse(response.json())`,
raising for a non-2xx status; it is `light`. For other request shapes, an adapter's `prepare`
returns an `HttpRequest(method, path, json=None, body=None, headers={})` and its `decode` takes
the response.

## TorchEngine

Runs on a device; the block below uses `devices=["cpu"]`, which needs no accelerator.

```python
import torch
from kohakunode import NodeLibrary

from togeari import Lane, Machine, Use
from togeari.engines.torch import TorchAdapter, TorchEngine, TorchModule
from togeari.io import Iterable, Memory
from togeari.policies import SizeOrWait

lib = NodeLibrary("dev")


@lib.node(outputs=["y"])
def project(x: list) -> list: ...


engine = TorchEngine(devices=["cpu"])
engine.add_model(
    "proj",
    TorchModule(torch.nn.Linear(4, 2), max_batch=16, example=[torch.zeros(4)], name="proj"),
)
lane = Lane([engine.unit("proj")], former=SizeOrWait(16, wait_ms=2), name="proj")
machine = Machine(
    program="@def (x: list)main(y):\n    (x)project(y)\n",
    entry="main",
    libraries=[lib],
    bind={"project": Use(lane, adapter=TorchAdapter(arg="x"))},
)
with machine:
    sink = Memory()
    machine.run(Iterable([{"x": torch.ones(4) * i} for i in range(5)]), sink)
    print(all(r.ok for r in sink.records), lane.batch_sizes)
    print(engine.unit("proj").load().extra["footprint"])
```

| Argument | Default | Meaning |
|---|---|---|
| `devices` | `("cuda:0",)` | the devices it owns, as the backend names them |
| `memory_cap_gb` | `None` | allocator cap per device, set before any weights load; under process isolation a device used by several placements gives each placement's process an equal share |
| `schedule` | `"priority_streams"` | `"priority_streams"`: one worker per module on a stream of its priority; `"co_resident"`: every module on the device, one worker, by priority then arrival; `"swappable"`: one module on the device at a time, its queue drains, then the oldest waiting module swaps in (sooner once a batch waited `swap_quantum_ms`) |
| `isolation` | `"thread"` | `"thread"`: devices on threads of the machine's process; `"process"`: each device in its own process (restarted when it dies), batches crossing by `transport` |
| `dtype` | (a dtype name) | the precision every module is loaded in |
| `slots` | 2 (8 under `swappable`) | batch slots per unit; `capacity()` is the free ones |
| `prep_workers` | 2 | threads building batches |
| `transport`, `shm_threshold` | `"shm"`, 1 MiB | process isolation: buffers at the threshold or above cross in shared memory |
| `start_timeout_s` | 300 | |
| `cancel_kill_s` | 5.0 | process isolation: a cancelled batch still running (or queued behind one) after this long has its device process killed and restarted, so the unit's slots come back; the device's other batches in flight fail with `WorkerDied`; `None` never kills |
| `revive_backoff_s` | `(1.0, 60.0)` | a device process that dies is restarted at once; when that fails the device is lost, and `ready()` tries again after a backoff from the first value, doubling up to the second |

Cancel under thread isolation: a batch not yet running is skipped; a running batch cannot be
stopped, and its slot stays busy until `forward` returns.

`add_model(name, model, priority=0, devices=None)` registers a module before the engine starts.
Each entry of `devices` is a placement: one of the engine's devices, or a tuple of them the
module is split across (default: the engine's first device). Every placement gives one batch unit: a
module on two devices is two units, a module split across two devices is one unit named by the
tuple (`torch:big@cuda:0+cuda:1`). `priority` orders it against the other modules of its
placement (a higher priority is served first: its own stream under `priority_streams`, earlier
in the queue under `co_resident`). `unit(name, device=None)` is its batch unit (`device`, a
device or a tuple, is needed when the module has several placements), which joins a batch lane
with a former; `units(name=None)` lists every unit, or every unit of one module.

Each placement runs on its own runtime (a thread set, or a process under process isolation):
one per device for the modules on that device alone, one per split tuple; a device no module
uses starts nothing. A split runs its streams on the tuple's first device; the cap is set and
the footprint measured on every CUDA device of the tuple; `engine.stats()` and `engine.backends`
are keyed by the placement name. `start()` leaves the calling thread's current CUDA device as it
was.

A module on several placements loads once per placement, each its own object. Under thread
isolation the first placement loads the registered module and every other placement a deep copy
made at the first start, before anything loads: one more host copy of the weights per
placement, and the module must be deep-copyable. Under process isolation each placement's
process unpickles its own copy. `unit(...).model` is the object that placement loads.

```python
import torch
from torch import nn

from togeari.engines.torch import TorchEngine, TorchModule

deep = nn.Sequential(nn.Linear(4, 64), nn.ReLU(), nn.Linear(64, 64), nn.ReLU(), nn.Linear(64, 2))
engine = TorchEngine(devices=["cpu"], dtype="float32")
engine.add_model("deep", TorchModule(deep, max_batch=16), devices=[("cpu", "cpu")])
print([u.name for u in engine.units("deep")])  # ['torch:deep@cpu+cpu']
```

 Every unit's `start()` starts the one engine: units
started at the same time wait for that single engine start and all get its error when it fails.
At the engine start every module is loaded, warmed up on its
`example()` batch and measured; the footprint (weights, peak, load and warm-up time) is in each
unit's `load().extra["footprint"]` and in `engine.stats()`. `spec()` is identity only, the
same before and after start. The engine wakes its lanes after a cancelled batch's reply and
after a device process restarts. A start that fails partway stops every device it already
started (their weights leave the device). `stop()` moves the weights to the CPU only once the
device's workers have ended; a forward still running after the join keeps them where they are.

`TorchModule(module, collate=stack, split=rows, max_batch=32, key=shape_key, example=(),
name=None, stages=None)` wraps a plain module: `collate` builds the batch, `forward` runs it on
the device, `split` gives the per-item results, `key(payload)` the shape bucket (items with equal
keys share a batch). Loaded on a tuple of devices, an `nn.Sequential` is split into stages: its
children in order, child `i` on the device `stages[i]` indexes in the tuple (default:
`stage_plan`, contiguous runs of near-equal parameter count, a child without parameters with its
predecessor); `forward` moves the activations to each stage's device, and the output stays on
the last stage's device. A custom `TorchModel` implements `load`, `to`, `example`, `prepare`,
`forward`, `split` and `key`; its `load(device, dtype)` and `to(device)` receive the tuple for a
split placement.

`TorchAdapter(arg=None)`: the payload is the node's inputs dict, or the one input named `arg`;
the reply is the node result. It is `light`, so a unit's `key()` reads the prepared payload on
the dispatcher; a heavy adapter's items share a batch only by its own `key(inputs, protocol)`
and run alone otherwise.
