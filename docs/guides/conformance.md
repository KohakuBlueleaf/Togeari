# Conformance

Every job of a machine must give what `kohakunode`'s reference interpreter gives for the same
job. The machine records a job's node calls in its sequential order; the reference then runs
the job with a hook that checks each call against the record. The code block on this page runs
as written.

## Recording

`record=<rate>` records that share of jobs (`record=1.0`: every job). A recorded job's
`JobRecord.record` holds one entry per node call, in sequential order: node, input digest,
`"ok"` or `"error"`, a copy of the result (or the error), and the values `mutates` inputs were
changed to. Recorded jobs also get every detail event.

## Checking in the same process

```python
from kohakunode import NodeLibrary

from togeari import Inline, Machine, Use
from togeari.conformance import conform
from togeari.io import Iterable, Memory
from togeari.testing import SimCall

lib = NodeLibrary("conf")


@lib.node(outputs=["y"])
def square(x: int) -> int:
    return x * x


PROGRAM = """
@def (xs: list)main(total):
    total = 0
    (xs)each(x, `one`)
    one:
        (x)square(s)
        (total, s)add(total)
"""

jobs = [{"xs": [1, 2, 3]}, {"xs": []}, {"xs": [4]}]
machine = Machine(
    program=PROGRAM,
    entry="main",
    libraries=[lib],
    bind={"square": Use(SimCall(name="sim", latency_ms=1)), "kir.std.*": Inline()},
    record=1.0,
)
with machine:
    sink = Memory()
    machine.run(Iterable(jobs), sink)
    job_program = machine.code.entries["main"].region.job

for rec in sorted(sink.records, key=lambda r: r.key):
    replayed = conform(job_program, rec, jobs[int(rec.key)])
    rerun = conform(job_program, rec, jobs[int(rec.key)], mode="reexecute", pure={"square"})
    print(rec.key, rec.outputs, replayed.ok, rerun.ok)
```

`conform(job_program, job_record, inputs, mode="replay", pure=None, engine_errors=True)`
returns a `Verdict(ok, reason, reference, machine, details)`:

| Mode | Each node call of the reference |
|---|---|
| `"replay"` | must match the record's node and input digest, and returns the recorded result (or raises the recorded error) |
| `"reexecute"` | the same; nodes named by `pure` (names or a predicate) run again and must give the recorded result |

Replay proves that every call happened in the sequential order with the inputs the sequential
run gives, and that the outputs follow from the results. Re-execution also catches a result
delivered to the wrong statement or job. Re-execute only nodes without side effects. Under
re-execution, `engine_errors=True` accepts a recorded failure the node itself does not raise
(an engine-side failure) and notes it in `details`; `False` makes it a mismatch.

## Checking a run afterwards

The `Recorder(path)` plugin (and `RunReport`, which embeds one) writes every recorded job to
disk, one folder per run, with the programs they ran:

```
records/
  programs/<hash>.json one file per program (by semantic hash): text, entry, passes, library references
  runs/<run>/run.json  the run's id, start time and digest providers
  runs/<run>/record.kv the job entries (record.pkl without KohakuVault)
```

```
togeari run machine.py --report records/
togeari conform records/
togeari conform records/ --run 20261020-101500-1234-ab12cd
togeari conform records/ --reexecute lookup
togeari conform records/ --lib shop_nodes:lib
```

`togeari run` writes records only with `--report DIR` (it adds a `RunReport`, which embeds a
recorder), and only for the jobs the machine's `record` knob samples.

| Option | Meaning |
|---|---|
| `--lib module:attr` | node libraries to import instead of the references the program files hold (repeatable) |
| `--run ID` | replay only these runs (repeatable); default: every run in the folder |
| `--reexecute NODE` | run this node again and compare with its record (repeatable) |
| `--max-fail N` | print at most N failures (20) |

A program file names each node library as `module:attr`, preferring a module that can be
imported later (a machine module run by `togeari run` is also registered under its file name).
The modules must be importable when `togeari conform` runs: run it from the machine's folder or
put that folder on `PYTHONPATH`. When they are not, it stops with
`cannot import the node libraries [...]: ...; put their module on PYTHONPATH or pass --lib
module:attr`. Before a run's first job it also imports the run's digest providers (the modules
that registered digest functions in that run), so input digests are computed as in the run.

It prints each failure with the reference's and the machine's outcome and the job's weak values
and weak digests, then one line per run and the total:

```
run 20261020-182106-108636-d20bd7: 3/3 conform; 0 with weak values; 0 weak digests
3/3 jobs conform; 0 with weak values; 0 weak digests
```

It exits 0 only when every job conforms. A weak value could not be stored by content (it does
not pickle); a weak digest is an input digested by type only. Give such types a
`__togeari_digest__()` method, or register a digest function with
`togeari.core.contracts.digest.register_digest(cls, fn)`.

## Reading records in Python

`Archive(path, runs=None)` (`togeari.plugins`) reads a record folder back: `programs` (by
semantic hash), `runs()` (run ids, oldest first), and `jobs()`, one dict per recorded job with
`run`, `key`, `entry`, `program`, `inputs`, `ok`, `outputs`, `failure`, `missing`, `stats`,
`record` and `weak`.

```python
from kohakunode import NodeLibrary

from togeari import Machine, Use
from togeari.io import Iterable, Memory
from togeari.plugins import Archive, Recorder
from togeari.testing import SimCall

lib = NodeLibrary("arch")


@lib.node(outputs=["y"])
def inc(x: int) -> int:
    return x + 1


machine = Machine(
    program="@def (x: int)main(y):\n    (x)inc(y)\n",
    entry="main",
    libraries=[lib],
    bind={"inc": Use(SimCall(name="sim", latency_ms=1))},
    record=1.0,
    plugins=[Recorder("runs/archive")],
)
with machine:
    machine.run(Iterable([{"x": i} for i in range(3)]), Memory())

archive = Archive("runs/archive")
print(archive.runs()[-1:], list(archive.programs))
for job in archive.jobs():
    print(job["key"], job["ok"], job["outputs"], [call[:3] for call in job["record"]])
```

Each call record is `(node, input digest, "ok" or "error", value, mutated values)`.

## `togeari doctor`

`togeari doctor` prints the interpreter, the OS and CPU count, the versions of Togeari, its
dependencies and the optional extras that are installed, the devices the device engine can use
with their free memory, and the engine worker processes still running: the builtin engines'
worker and device processes register themselves while they run, and doctor lists the ones
whose parent has exited (left over from an earlier run) and removes the entries of processes
that no longer exist. Run it first when a machine behaves differently on two
hosts.

The test suite runs the same check on generated programs under many machine configurations
(see [generated programs](../internals/verification/generated-programs.md)).
