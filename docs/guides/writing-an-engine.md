# Writing an engine

An engine is any object that keeps one of the three contracts in
`togeari.core.contracts.engine`. The machine never asks how it runs; it only calls the contract
from the dispatcher thread and receives results through `done`. The code blocks on this page
run in order as one script.

The machine keeps all state in the jobs' values and guarantees nothing about state outside
them. Calls of different jobs run concurrently on any unit, in any order, and `ordered` orders
only one job's effects. An engine or node may keep state across calls (an adaptive strategy, a
cache, a connection, a shared file); whether that state changes results, and locking it, is the
engine's responsibility.

## The contract

| Member | Called | Must |
|---|---|---|
| `kind` | — | `"call"`, `"batch"` or `"stream"` |
| `name` | — | unique among the machine's units |
| `protocol` | — | the payload family its adapters speak (`"python"`, `"http"`, ...); `None` takes the machine's default |
| `shares_memory` | — | `True` when it runs in the machine's process and sees the caller's objects |
| `start()`, `stop()` | construction, `machine.stop()` | connect, spawn, load; may be slow (run off the dispatcher) |
| `ready()` | while starting, and polled with backoff while the unit is down | report health |
| `spec()` | when the unit joins a lane | report configuration, not automatic portable revision identity; see [Execution identity](execution-identity.md). Counters and footprints go in `load().extra` |
| `capacity()` | each pump | how many more submits it accepts now |
| `load()` | admission, selectors, reports | `Load(running, waiting, extra)` |
| `submit(work, done)` | the pump | hand the work to the engine's own threads and return a ticket at once |
| `cancel(ticket)` | a squash, a timeout, a detach | best effort |
| `wake_at()` (optional) | while the lane has queued work | the `time.monotonic()` time capacity may rise without a completion (a rate limit refilling) |
| `add_wake(fn)` (optional) | when the unit joins a lane | remember `fn`; call it (from any thread) whenever capacity rose without a completion |

`EngineBase` gives defaults for all of these, including `add_wake` and `wake()` (calls every
added function).

| Kind | One submit | Who queues / batches | `capacity()` |
|---|---|---|---|
| `call` | one item | the lane queues | free concurrent slots |
| `batch` | a list of up to `max_batch` items with equal `key(item)` | the lane queues and forms batches | free batch slots |
| `stream` | one item | the engine itself | a soft cap |

## A call engine

Each worker thread takes an item, runs its payload and answers exactly once. The slot is
counted free **before** the answer goes out, so a `capacity()` read after the answer already
sees it.

```python
import queue
import threading

from togeari import EngineBase, ItemError, Load, Ok, UnitError


class ThreadedEngine(EngineBase):
    """Runs `PythonAdapter` payloads on its own worker threads."""

    kind = "call"
    protocol = "python"
    shares_memory = True

    def __init__(self, workers: int = 4, name: str = "threaded"):
        super().__init__(name)
        self.workers = workers
        self.lock = threading.Lock()
        self.running = 0
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        for i in range(self.workers):
            t = threading.Thread(target=self.serve, name=f"{self.name}-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        for _ in self.threads:
            self.inbox.put(None)
        for t in self.threads:
            t.join(5)
        self.threads.clear()

    def ready(self) -> bool:
        return bool(self.threads)

    def spec(self) -> dict:
        return {"engine": "ThreadedEngine", "name": self.name, "workers": self.workers}

    def capacity(self) -> int:
        with self.lock:
            return self.workers - self.running

    def load(self) -> Load:
        with self.lock:
            return Load(running=self.running, waiting=self.inbox.qsize())

    def submit(self, work, done):
        with self.lock:
            self.running += 1
        self.inbox.put((work, done))
        return work

    def serve(self) -> None:
        while (job := self.inbox.get()) is not None:
            item, done = job
            try:
                result = Ok(item.finish(item.payload().run()), True)
            except ConnectionError as exc:
                result = UnitError(exc)
            except Exception as exc:
                result = ItemError(exc)
            with self.lock:
                self.running -= 1
            done(item, [result])
```

- `item.payload()` runs the adapter's `prepare` once (any thread); `item.finish(raw)` runs its
  `decode`. `Ok(value, True)` says the value is already decoded; `Ok(raw)` leaves decoding to
  the machine.
- `cancel` is inherited (a no-op): the item then gets its normal result, which the contract
  allows. An engine that can stop work answers `Cancelled()` instead.
- `UnitError(exc, retry_after=None)` takes the unit out of service until `ready()` is true
  again; the lane retries the item elsewhere. `ItemError` fails only the item.

## Invocation and attempt identity

Machine-owned work items expose read-only `job_execution_id`, `invocation_id` and
`attempt_id` properties. The first two identify the logical run and dynamic call; retries
keep them. Each actual per-item `submit` gets a fresh attempt ID, including a submit that
raises. Each member of a batch has its own attempt ID. Queue waiting, plugin blocking,
expiry before submission and cache hits create no new attempt.

Logical IDs are available before `pre_submit`, even without recording. The attempt ID is
assigned after plugin transforms and deadline checks, just before your engine is called.
On a retry hook it may still show the previous actual attempt until the next submit occurs.
These are correlation tokens, not a promise of deterministic sampling or exactly-once
external effects; equal payloads remain separate requests.

The owning inflight is authoritative. After the complete pre-submit plugin chain, both
same-object mutations and replacement items have their owner and ID transport restored.
Spoofed ID entries in `ext` are not another identity channel. Protection is at the engine
and record boundary, not between individual plugins in the chain. Internal identity
setters are runtime machinery, not plugin APIs. There is no public mutable attempt-history
list on a work item. A machine's `pre_submit` replacement must be an exact `WorkItem`,
not a subclass overriding transport properties or methods; otherwise the item fails with
`Blocked` before submission. Migrate plugin subclasses to base work items with plugin data
in `ext`. Standalone lane contracts outside a machine are not restricted by this boundary.

Ownerless `WorkItem`s constructed for engine tests may have logical IDs set to `None`;
standalone lane submissions still receive fresh attempt IDs. See
[Invocation identity](invocation-identity.md) for record sidecars and restore semantics.

## Behaviour every engine keeps

| Rule | Meaning |
|---|---|
| B1 | `submit` never blocks and never runs the work on the caller |
| B2 | exactly one result per item, through `done`, from any thread |
| B3 | `submit`, `cancel`, `capacity`, `load`, `key`, `wake_at` are called from the dispatcher thread only; `done` is never called under a lock those methods take |
| B4 | capacity is honest; work past it is still answered |
| B5 | after `cancel`, the result is the normal one or `Cancelled()` |
| B6 | a batch engine's result for an item does not depend on its batch mates |
| B7 | `ItemError`: this item failed. `UnitError`: the whole unit is blocked |
| B8 | engines see work items only; equal payloads are separate requests |

`check_engine` drives an engine from one thread the way a dispatcher does and checks B1–B7 (B8
is not visible through the engine interface; one answer per item is B2):

```python
from togeari import WorkItem
from togeari.adapters import PythonAdapter
from togeari.testing import check_engine


def half(x: int) -> float:
    if x < 0:
        raise ValueError("negative")
    return x / 2


samples = [WorkItem("half", {"x": n}, PythonAdapter(half)) for n in range(4)]
failing = [WorkItem("half", {"x": -1}, PythonAdapter(half))]
report = check_engine(lambda: ThreadedEngine(workers=2), samples, failing_items=failing, rounds=50)
print(report)
report.raise_if_failed()
```

| `check_engine` argument | Default | Meaning |
|---|---|---|
| `failing_items` | `()` | items that must give `ItemError` |
| `kill` | `None` | a function that kills the backend; the next submit must give `UnitError` |
| `tolerance` | `None` | relative tolerance, or `fn(a, b)`, for output comparisons (B6) |
| `submit_bound_ms`, `submit_quantile`, `submit_max_ms` | 2.0, 0.99, 50.0 | Legacy diagnostic references only; timing never affects the behavioral verdict |
| `rounds`, `cancel_rate` | 200, 0.25 | submits and the share cancelled |
| `timeout_s`, `grace_s`, `seed` | 30, 0.2, 0 | answer wait, late-duplicate grace, RNG seed |
| `kill_wait_s` | 2.0 | after `kill()`, how long the engine has to notice |
| `probe_s` | 1.0 | how long `done` is held open while the driver calls the engine (B3) |

`togeari units machine.py` runs it on every unit of a machine module that gives
`unit_samples` (unit name → `WorkItem`s or `(node, inputs)` pairs), and optionally
`unit_factories` and `unit_check_args`.

## Waking the lane

A lane pumps when something changes: an answer, an enqueue, a unit state change. Two optional
methods cover capacity that rises otherwise:

- `wake_at()` returns when capacity may rise at a known time; the dispatcher sleeps until then.
- `add_wake(fn)`: call every added `fn()` when capacity rose at an unknown time without an
  answer (a worker replaced, a cancelled item's slot freed). With `EngineBase`, call
  `self.wake()`.

An engine whose capacity only rises with its answers needs neither.

## Batch engines and adapters

A batch engine sets `kind = "batch"`, `max_batch`, and `key(item)`: items with equal keys may
share a batch. `key()` runs on the dispatcher, so it may read `item.payload()` only when the
item's adapter is `light`; a heavy adapter's `prepare` must run on the engine's threads.

An adapter turns a node's inputs into the engine's payload and the reply into the node's
result:

| Member | Meaning |
|---|---|
| `protocols` | the engine protocols it speaks |
| `light` | cheap enough to run on the dispatcher |
| `check(signature)` | construction problems for a node |
| `prepare(inputs, protocol)` | the payload, through `item.payload()` |
| `decode(raw, protocol)` | the node result as the node function returns it, through `item.finish(raw)` |

Builtins (`togeari.adapters`): `PythonAdapter` (the node function, as a `PythonCall` payload),
`JsonHttp` (JSON over HTTP for `WebCallEngine`), and the adapter of the builtin device engine.
