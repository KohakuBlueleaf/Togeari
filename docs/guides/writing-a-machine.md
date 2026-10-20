# Writing a machine

A machine is one or more KIR programs, a binding for every node they call, and the lanes of
engine units the bindings name. The code blocks on this page run in order as one script.

## 1. Nodes

Node functions live in a `NodeLibrary`. Signatures come from the function; `outputs=` names
the outputs; `ordered=` and `mutates=` are the node flags.

```python
from kohakunode import NodeLibrary

lib = NodeLibrary("shop")
PRICES = {"apple": 1.5, "pear": 2.0}


@lib.node(outputs=["price"])
def lookup(item: str) -> float:
    return PRICES[item]


@lib.node(outputs=["total"])
def total(prices: list) -> float:
    return sum(prices)


@lib.node(ordered=True)
def log(line: str) -> None:
    print(line)
```

## 2. The program

`program=` takes KIR text as a `str`, or a file as a `Path` (any `os.PathLike`). A `str` is
always program text, never a file name.

```python
ORDER = """
@def (items: list, coupon: str = "")order(total):
    prices = []
    (items)each(item, `one`)
    one:
        (item)lookup(price)
        (prices, price)append(prices)
    (prices)total(total)
    ("order priced")log()
"""
```

`togeari decode order.kir --entry order --lib shop_nodes:lib` prints what the machine compiles:
addresses, reconvergence points, may-write sets, carried names, `@try` regions.

Several programs: `programs={Path("a.kir"): ["main"], Path("b.kir"): ["other"]}`. Each listed
entry is a top-level `@def`; `None` means the top level. `entry=` names the default entry; a
source item's `entry` picks another.

## 3. Bindings and lanes

Every called node needs a binding, by exact name or glob (`kir.std.*` covers the standard
library; an exact name beats a glob, a longer glob a shorter one, a tie is an error):

| Binding | Runs |
|---|---|
| `Inline(warn_us=None)` | the node's function on the dispatcher; for microsecond work and `kir.std` |
| `Use(lane, adapter=None, store=None, idempotent=False)` | on the lane's units; no adapter means the machine's default (`PythonAdapter`: the node's own function) |
| `Use(engine)` | shorthand for a one-unit lane |

`store="content"` serves repeated inputs from the machine's store; `store="positional"` serves
the same job's same statement on a rerun. `idempotent=True` lets an `ordered` node retry on
another unit after a unit error.

```python
from togeari import Inline, Lane, Machine, Use
from togeari.engines.python import PythonCallEngine
from togeari.policies import Fixed, LeastLoaded

pool = Lane(
    [PythonCallEngine(workers=4, mode="thread", name="cpu0")],
    select=LeastLoaded(),
    name="cpu",
)
machine = Machine(
    program=ORDER,
    entry="order",
    libraries=[lib],
    bind={"lookup": Use(pool), "total": Inline(), "log": Inline(), "kir.std.*": Inline()},
    admission=Fixed(128),
    window=32,
)
```

Policies are passed as objects (`admission=Fixed(128)`, `issue=OldestJobFirst()`, a lane's
`former=` and `select=`), or as the name of a policy a plugin contributes
(`admission="wide"`, see [writing a plugin](writing-a-plugin.md)).

Construction checks everything at once and raises one `ConstructionError` listing every
problem: unknown or unbound nodes, ties between patterns, adapters that cannot speak a unit's
protocol, `mutates` nodes on units that copy values, lanes that mix kinds, formers on the wrong
lanes, duplicate unit names, invalid knobs, and units that fail to start.

```python
from togeari import ConstructionError

try:
    Machine(program=ORDER, entry="order", libraries=[lib], bind={"lookup": Inline()}, window=0)
except ConstructionError as exc:
    print(exc)
```

## 4. Machine arguments and knobs

| Argument | Default | Meaning |
|---|---|---|
| `programs`, `program`, `entry` | — | the programs and the default entry |
| `libraries`, `sig_path`, `passes` | — | node libraries, signature folders, lowering passes |
| `bind` | — | node-name patterns to bindings |
| `admission`, `issue` | `Fixed(64)`, `OldestJobFirst()` | policies |
| `store` | `None` | results and snapshot store (`togeari.stores.MemoryStore`, ...) |
| `plugins` | `[]` | plugin instances |
| `adapter`, `protocol` | `PythonAdapter()`, `"python"` | the adapter of a `Use` without one; the protocol of a unit without one |
| `start_timeout_s` | 120 | per unit start |
| `start_units` | `True` | `False` builds and checks without starting units |
| `source_buffer`, `sink_buffer` | 1024, 1024 | bounded source and sink queues; admission pauses while the sink queue is full |
| `seed` | 0 | record and event sampling |

Every other keyword is a knob. Knobs are validated at construction and by `set_knob`.

| Knob | Default | Meaning |
|---|---|---|
| `window` | 64 | unfinished statements per job |
| `retire_buffer` | 4096 | finished statements per job waiting to retire |
| `run_ahead` | `True` | strands run past undecided branches |
| `mutates` | `"copy-if-speculative"` | `"wait"`, `"copy-always"`: how `mutates` nodes treat older unfinished work |
| `inline_warn_us` | 50 | warn when an inline node's median time is above this |
| `snapshot_every` | `None` | snapshot each job every N retired statements (needs a store) |
| `record` | `None` | share of jobs whose node calls are recorded for conformance |
| `record_limit` | 1 000 000 | recorded calls per job before its record is dropped |
| `events` | `"sampled"` | `"all"`, `"sampled"`, `"counters"`: bus event detail |
| `event_sample` | 0.1 | share of jobs whose detail events are sampled while `record` is off |
| `fingerprint_inputs` | `False` | fail a node without `mutates` that changes an input |
| `assert_alias` | `False` | check at retire and job end that no value is unfilled or shared between jobs |
| `step_budget` | `None` | instructions a job may execute before it fails |
| `plugin_errors` | `"disable"` | `"log"` keeps a plugin enabled after its error |
| `gc_threshold` | `(50000, 20, 100)` | the process collector threshold while the machine runs; `None` keeps the process's own |
| `squash_reuse` | 1024 | completed results kept per job when a fault squashes them; a refetched call with the same inputs reuses one instead of running again; 0 turns it off |

## 5. Running

```python
from togeari.io import Iterable, Memory

orders = [{"items": ["apple", "pear"]}, {"items": ["pear"], "coupon": "X"}, {"items": ["kiwi"]}]
with machine:
    sink = Memory()
    summary = machine.run(Iterable(orders), sink)
    for rec in sorted(sink.records, key=lambda r: r.key):
        print(rec.key, rec.ok, rec.outputs, rec.failure)

    handle = machine.submit({"items": ["apple"]}, key="extra")
    print(handle.result(timeout=10).outputs)
    print(machine.inspect("jobs").result())
    print(machine.command("set_knob", name="window", value=64).result())
```

| Call | Does |
|---|---|
| `machine.run(source, sink, limit=None)` | runs until the source is drained and every job has ended; returns a summary; raises `DispatcherDied`, or the source's or sink's own error, after the run |
| `machine.submit(inputs, key=None, entry=None)` | a `JobHandle`; `handle.result(timeout)` waits for the `JobRecord`; a key already resident is rejected |
| `machine.inspect(name, **args)` | a query answered between cycles; `.result()` waits |
| `machine.command(name, **args)` | a command applied between cycles: `set_knob`, `load`, `attach`, `detach`, `cancel_job`, `pause_job`, `resume_job`, `plugin_add`, ... |
| `machine.load(program, entries)` | another program while running; its nodes are checked against the bindings |
| `machine.attach(lane, unit)`, `machine.detach(unit, drain=True)` | a unit joins or leaves a lane while running |
| `machine.stop(drain=True)` | stops admitting; `drain=True` finishes resident jobs, `drain=False` cancels them; stops plugins and units |

A `JobRecord` holds `key`, `entry`, `ok`, `outputs`, `failure` (`{kind, node, message}`),
`missing` outputs, `stats` and `record` (the conformance record when sampled). The third order
above fails with `KeyError` from `lookup`: a failure outside `@try` fails the job, and its
record says which node and why.

Sources: `Iterable(items, entry=None, key=None)`, `Jsonl(path, entry=None, key=None)`,
`Folder(pattern, field="path")`; sinks: `Memory()`, `Jsonl(path)` (`togeari.io`). Without
`key`, `Iterable` and `Jsonl` key jobs by their position as a string (`"0"`, `"1"`, …; string
order, so `"10"` sorts before `"2"`). A source yielding plain mappings, and `submit` without
`key`, get unique strings from a process-wide counter, which differ between runs. Give explicit
keys when snapshots or positional stores must match jobs across runs.

**Errors.** An error inside the machine while working on one job fails that job
(`MachineError`); a plugin that raises is disabled; an error in a policy stops the dispatcher.
Then every waiting handle and reply is answered with `DispatcherDied`, `run()` raises it, and
every later call raises it.

## 6. Checking without running

```python
from togeari import no_unit_start

with no_unit_start():
    checked = Machine(
        program=ORDER,
        entry="order",
        libraries=[lib],
        bind={"lookup": Use(pool), "total": Inline(), "log": Inline(), "kir.std.*": Inline()},
    )
print(checked.warnings, list(checked.code.entries))
checked.stop()
```

Machines built inside `no_unit_start()` are checked completely but start no unit and cannot
run. `togeari check machine.py` builds a machine module this way. A machine module is a Python
file whose `build()` returns `(machine, source, sink)`; `togeari run machine.py` runs it.

Next: [tuning](tuning.md), [conformance](conformance.md).
