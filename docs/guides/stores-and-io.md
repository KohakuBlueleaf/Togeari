# Stores, sources and sinks

A machine reads jobs from a source, writes one record per job to a sink, and may keep results
and snapshots in a store. All three are small contracts (`togeari.core.contracts`); the
builtins are below. Each code block on this page runs on its own.

## Stores

A store keeps node results by key for two binding modes, and job snapshots:

| Use | Key | Written |
|---|---|---|
| `Use(..., store="content")` | region, node, inputs digest, adapter and engine identity | when the call completes |
| `Use(..., store="positional")` | region, job key, job inputs digest, the statement's address in its region and its order key | when the call retires |
| `snapshot_every=N` | region, job key, job inputs digest | every N retired statements; deleted when the job ends ok |

The contract:

| Method | Must |
|---|---|
| `get(key)` | return a fresh copy of the stored value, or `MISS` (`togeari.core.contracts.MISS`) |
| `put(key, value)` | keep its own copy, so no later change to the value reaches it and no two jobs share a stored object |
| `delete(key)` (optional) | remove the key; without it the machine stores `None`, which reads as a miss |

`get` and `put` are called from the dispatcher thread. A store error is reported (a
`store.error` event) and counted; a failed `get` is a miss, and a failed snapshot read starts
the job from its beginning.

**Trust.** A snapshot is a pickle of the job's state, and resuming unpickles what the store
returns; unpickling runs code the data names. A store used with `snapshot_every` must therefore
be writable only by trusted writers (a shared `Vault` directory is as trusted as everyone who
can write to it). Node results under `store=` are kept by the store's own format (`Vault`
pickles them too).

```python
from kohakunode import NodeLibrary

from togeari import Machine, Use
from togeari.io import Iterable, Memory
from togeari.stores import MemoryStore
from togeari.testing import SimCall

lib = NodeLibrary("cache")


@lib.node(outputs=["y"])
def slow(x: int) -> int:
    return x * 10


store = MemoryStore()
for run in range(2):
    machine = Machine(
        program="@def (x: int)main(y):\n    (x)slow(y)\n",
        entry="main",
        libraries=[lib],
        bind={"slow": Use(SimCall(name="sim", latency_ms=5), store="content")},
        store=store,
    )
    with machine:
        machine.run(Iterable([{"x": i % 3} for i in range(9)]), Memory())
        stats = machine.inspect("stats").result()
    print(run, stats["store_hits"], stats["store_misses"], len(store))
```

| Store | Keeps |
|---|---|
| `MemoryStore()` (`togeari.stores`) | a dict in memory; copies in and out (`__togeari_copy__` or a deep copy) |
| `VaultStore(path)` (`togeari.stores.vault`, extra `[vault]`) | a KohakuVault database on disk, kept across runs; values pickled, every `get` unpickles a fresh copy |

A store of your own needs only the three methods:

```python
import copy
import threading

from togeari.core.contracts import MISS


class DictStore:
    """Results in a dict, copied in and out."""

    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            value = self.data.get(key, MISS)
        return value if value is MISS else copy.deepcopy(value)

    def put(self, key, value):
        value = copy.deepcopy(value)
        with self.lock:
            self.data[key] = value

    def delete(self, key):
        with self.lock:
            self.data.pop(key, None)


store = DictStore()
store.put(b"k", [1, 2])
print(store.get(b"k"), store.get(b"missing") is MISS)
```

Content keys need inputs that digest by content: a value that does not pickle digests by type
only (weak), and such a call skips the store. Positional keys and snapshots match a job by key
and inputs, so give explicit keys (below).

## Sources

A source is an iterable of `SourceItem(key, inputs, entry=None)` with `shard(rank, world)`. A
plain mapping from any iterable becomes a `SourceItem` keyed by its position.

| Source (`togeari.io`) | Jobs |
|---|---|
| `Iterable(items, entry=None, key=None)` | one per dict (or `SourceItem`); the key is `item[key]`, or the position |
| `Jsonl(path, entry=None, key=None)` | one per line; `__key__` and `__entry__` fields set the key and the entry |
| `Folder(pattern, field="path", entry=None)` | one per file matching the glob; the path is the key and the `field` input |
| `Vault(path, entry=None, field="value")` (`togeari.io.vault`, extra `[vault]`) | one per key of a KohakuVault database; a dict value is the inputs, any other value goes into `field` |

`shard(rank, world)` keeps every `world`-th item starting at `rank`, so `world` machines can
split one source.

## Sinks

A sink has `write(record)` and `close()`; the machine calls them from its sink writer thread.

| Sink (`togeari.io`) | Writes |
|---|---|
| `Memory()` | records in `.records`; `by_key()`, `outputs()` |
| `Jsonl(path)` | one JSON line per record: key, entry, ok, outputs, failure, missing |
| `Vault(path)` | pickled records by job key (without their conformance record); `records()` reads them back |

```python
from pathlib import Path

from togeari import Inline, Machine
from togeari.io import Iterable, Jsonl, Memory

Path("runs/io").mkdir(parents=True, exist_ok=True)
Path("runs/io/in.jsonl").write_text(
    '{"__key__": "a", "items": [1, 2]}\n{"__key__": "b", "items": []}\n', encoding="utf-8"
)
machine = Machine(
    program="@def (items: list)main(n):\n    (items)len(n)\n",
    entry="main",
    bind={"kir.std.*": Inline()},
)
with machine:
    machine.run(Jsonl("runs/io/in.jsonl"), Jsonl("runs/io/out.jsonl"))
    half = Memory()
    machine.run(Iterable([{"items": [1]}, {"items": [1, 2]}], key=None).shard(0, 2), half)
print(Path("runs/io/out.jsonl").read_text(encoding="utf-8"))
print([(r.key, r.outputs) for r in half.records])
```

## Keys

Every job has a key; keys must be unique among resident jobs (a submit or source item whose key
is resident fails as `DuplicateKey`). Without a key field, keys are positions (`"0"`, `"1"`,
...) and are marked as automatic. Snapshots and positional store results match a job by its
key and inputs, so a run that uses them should give explicit keys; with automatic keys the
machine logs one warning.
