# The cores compared

The Python core (the reference) against the Rust core at every `gil_offer_us` value
([the GIL](../internals/native/gil.md)), measured in one run of
`python scripts/studies/p8/s8.py --scale full`. The run writes `runs/p8/s8/full/`: `s8.json`,
`summary.md` (these tables), every child's results and log.

## Method

- **Machine:** one Windows 11 workstation (172 hardware threads), CPython 3.13, one GPU
  (`cuda:0`, allocator capped per process); togeari 0.2.0 before its defaults were set, both
  cores built in release mode.
- **Variants:** `python`; `rust-oN` = the Rust core with `gil_offer_us=N` for N in 0, 20, 100,
  300, 1000, 5000, `inf`, and `rust-onone` with `None`.
- **Interleaving:** every measurement runs each variant once per round, in an order that
  rotates between rounds, so drift in the machine falls on every variant alike.
- **Dispatcher ceiling** (`scripts/benches/dispatcher_ceiling.py`): simulated engines that
  answer at once, so the dispatcher is the only cost; five rounds; with the default
  `gc_threshold` and with the process's own.
- **Scaling** (`scripts/benches/scaling.py`): dispatcher µs per resident job and per `each`
  item at three sizes; three rounds, a warm-up run discarded in each.
- **P6 studies A and B** (`scripts/studies/p6/run.py`): event levels and observers (none,
  `Counters`, `RunReport`), the knob defaults, `examples/mixed` and `examples/trace_replay`;
  three rounds.
- **GIL contention** (`scripts/studies/p8/gil.py`): an engine fed directly from its own thread
  (a torch network, or pure-Python work; in a thread or a worker process) while the
  dispatcher runs a chain workload paced at 100, 300 and 1000 jobs/s and at its ceiling; the
  engine's items/s, the dispatcher's calls/s and busy share, and the core's GIL offers/s in the
  same window; three rounds.
- **Parity first:** the trace parity suite (824 tests) passed before any measurement.

## The decision rule

Written before the run and applied by `scripts/studies/p8/summary/decide.py`:

- For every shape the run plans: ratio = variant median / `python` median (inverted for times,
  so above 1 is faster); spread = the sum of both medians' relative half-ranges over the
  rounds. A tie when |ratio − 1| ≤ max(spread, 3%), else faster or slower; a shape without both
  medians is missing. Latency-bound shapes (`trace_replay`) are ties by construction; the idle
  GIL load is left out.
- A variant fails when any of its children did not exit 0 or timed out, or any job, round,
  configuration or phase failed; a failed variant cannot be chosen.
- **Default `gil_offer_us`:** the Rust variant with no failure, 0 slower and 0 missing shapes
  and the most faster ones; ties go to the higher torch / process items/s at the ceiling, then
  to the sweep order.
- **Default core:** `"auto"` runs Rust only if that variant exists and parity passed.

## Outcome

No failure and no missing shape in any variant. Against `python`, over 100 shapes:

| variant | faster | ties | slower |
|---|---|---|---|
| `rust-onone` | 84 | 16 | 0 |
| `rust-oinf` | 83 | 14 | 3 |
| `rust-o300` | 80 | 20 | 0 |
| `rust-o100` | 79 | 21 | 0 |
| `rust-o20` | 79 | 20 | 1 |
| `rust-o5000` | 77 | 23 | 0 |
| `rust-o0` | 76 | 23 | 1 |
| `rust-o1000` | 76 | 24 | 0 |

So `gil_offer_us` defaults to `None` and `core="auto"` runs the Rust core. The slower shapes:
`rust-o0` and `rust-o20` on an inline chain with `RunReport` at `"all"` (0.80× and 0.78×:
each offer hands the GIL to the report's threads, and the dispatcher waits out their run);
`rust-oinf` on Python work in an engine thread (0.90–0.94×: every region holds the GIL).

![Dispatcher ceiling by core](../assets/cores-ceiling.svg)

![Dispatcher cost as the machine grows, by core](../assets/cores-scaling.svg)

![Engine throughput under dispatcher load, by core](../assets/cores-gil.svg)

**Reading the GIL tables.** Below its ceiling the dispatcher waits for events between jobs,
and each wait releases the GIL, so every Rust variant feeds the engines about equally: a torch
network in a worker process keeps 6,808–7,037 items/s with the dispatcher at 1000 jobs/s,
against 895 on the Python core. At its ceiling the dispatcher never waits; with
`gil_offer_us` `None`, `inf`, `1000` or `5000` other threads then get the GIL only at the
interpreter's switch interval (537–562 items/s), while `0`–`300` offer it 850–3,200 times a
second and keep 1,739–2,020 items/s, the dispatcher still running about 40,000 calls/s. The
offers cost the dispatcher most where an observer's threads take every one: against `None`,
`300` runs inline chains with `RunReport` at 0.82–0.91× and `0` at 0.55–0.64×
([tuning](../guides/tuning.md#the-native-core)).

Below: every table of the run's `summary.md`. "spread" is the noise band of a shape;
`min..max` the range over rounds.

## Every shape against its baseline

### rust-o0 vs python

76 faster, 23 ties, 1 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | python | rust-o0 |
|---|---|---|---|---|
| P6 A cpu chain-inline all report calls/s | 0.801 | ±7.5% | 25,833.9 (25,815.9..27,572.6) | 20,705.6 (20,651.0..22,362.3) |

### rust-o20 vs python

79 faster, 20 ties, 1 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | python | rust-o20 |
|---|---|---|---|---|
| P6 A cpu chain-inline all report calls/s | 0.776 | ±12.2% | 25,833.9 (25,815.9..27,572.6) | 20,039.6 (19,826.4..23,336.6) |

### rust-o100 vs python

79 faster, 21 ties, 0 slower, 0 missing.

### rust-o300 vs python

80 faster, 20 ties, 0 slower, 0 missing.

### rust-o1000 vs python

76 faster, 24 ties, 0 slower, 0 missing.

### rust-o5000 vs python

77 faster, 23 ties, 0 slower, 0 missing.

### rust-oinf vs python

83 faster, 14 ties, 3 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | python | rust-oinf |
|---|---|---|---|---|
| GIL python / thread engine items/s at 100/s | 0.938 | ±5.0% | 380.8 (379.7..392.2) | 357.1 (345.3..368.9) |
| GIL python / thread engine items/s at 1000/s | 0.900 | ±6.5% | 389.9 (368.6..393.9) | 351.0 (347.1..370.3) |
| GIL python / thread engine items/s at ceiling | 0.916 | ±5.6% | 389.3 (355.6..389.6) | 356.6 (353.6..362.7) |

### rust-onone vs python

84 faster, 16 ties, 0 slower, 0 missing.

### rust-o20 vs rust-o0

3 faster, 95 ties, 2 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | rust-o0 | rust-o20 |
|---|---|---|---|---|
| P6 A cpu chain-inline sampled none calls/s | 0.913 | ±5.9% | 273,272.6 (267,732.4..281,005.0) | 249,417.0 (248,713.4..265,856.7) |
| P6 A cpu each-batch sampled report calls/s | 0.951 | ±3.8% | 43,974.9 (43,168.9..45,693.6) | 41,828.8 (41,301.9..42,062.8) |

### rust-o100 vs rust-o0

11 faster, 87 ties, 2 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | rust-o0 | rust-o100 |
|---|---|---|---|---|
| P6 A cpu chain-call sampled counters calls/s | 0.940 | ±3.5% | 39,087.7 (38,610.4..39,164.9) | 36,743.8 (36,242.9..38,287.4) |
| P6 A cpu chain-call counters none calls/s | 0.917 | ±2.6% | 53,730.6 (52,513.1..55,138.9) | 49,281.2 (49,159.3..49,358.7) |

### rust-o300 vs rust-o0

25 faster, 73 ties, 2 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | rust-o0 | rust-o300 |
|---|---|---|---|---|
| P6 A mixed all none calls/s | 0.900 | ±6.8% | 4,942.1 (4,725.1..5,343.2) | 4,447.7 (4,434.0..4,484.8) |
| GIL python / process engine items/s at ceiling | 0.927 | ±4.3% | 1,805.7 (1,781.1..1,855.2) | 1,673.3 (1,617.8..1,694.2) |

### rust-o1000 vs rust-o0

27 faster, 66 ties, 7 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | rust-o0 | rust-o1000 |
|---|---|---|---|---|
| P6 A cpu chain-call sampled counters calls/s | 0.956 | ±3.2% | 39,087.7 (38,610.4..39,164.9) | 37,352.9 (36,454.8..38,339.3) |
| P6 A cpu chain-call counters none calls/s | 0.926 | ±7.4% | 53,730.6 (52,513.1..55,138.9) | 49,729.0 (48,629.7..53,595.7) |
| P6 A cpu each-batch counters counters calls/s | 0.929 | ±5.6% | 86,573.4 (81,852.4..87,858.0) | 80,456.4 (79,753.1..83,125.2) |
| P6 B each-batch window 256 calls/s | 0.888 | ±7.7% | 87,745.1 (84,182.9..88,317.6) | 77,908.3 (77,074.8..85,387.5) |
| GIL torch / thread engine items/s at ceiling | 0.682 | ±14.7% | 562.5 (511.4..639.4) | 383.7 (383.5..409.3) |
| GIL torch / process engine items/s at ceiling | 0.309 | ±15.1% | 1,739.1 (1,661.0..2,018.6) | 537.1 (536.7..588.3) |
| GIL python / process engine items/s at ceiling | 0.945 | ±3.8% | 1,805.7 (1,781.1..1,855.2) | 1,706.7 (1,659.1..1,717.9) |

### rust-o5000 vs rust-o0

30 faster, 61 ties, 9 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | rust-o0 | rust-o5000 |
|---|---|---|---|---|
| P6 A cpu each-batch sampled none calls/s | 0.907 | ±9.2% | 107,217.7 (106,652.6..116,270.9) | 97,196.3 (91,568.7..100,746.3) |
| P6 A cpu each-batch counters counters calls/s | 0.865 | ±7.5% | 86,573.4 (81,852.4..87,858.0) | 74,924.3 (73,603.0..79,695.6) |
| P6 A cpu each-batch counters report calls/s | 0.900 | ±4.5% | 71,735.5 (68,744.6..71,889.7) | 64,551.1 (63,177.4..66,111.8) |
| P6 B each-batch defaults calls/s | 0.924 | ±6.3% | 81,439.8 (80,721.9..83,257.0) | 75,215.2 (70,137.8..77,244.3) |
| P6 B each-batch window 256 calls/s | 0.921 | ±7.3% | 87,745.1 (84,182.9..88,317.6) | 80,795.1 (74,174.6..82,084.3) |
| P6 B each-batch TargetLoad calls/s | 0.954 | ±3.7% | 81,465.3 (79,088.3..82,884.4) | 77,750.7 (77,153.7..79,242.8) |
| GIL torch / thread engine items/s at ceiling | 0.727 | ±14.5% | 562.5 (511.4..639.4) | 409.0 (383.5..409.1) |
| GIL torch / process engine items/s at ceiling | 0.323 | ±12.5% | 1,739.1 (1,661.0..2,018.6) | 562.3 (537.2..562.5) |
| GIL python / process engine items/s at ceiling | 0.949 | ±4.1% | 1,805.7 (1,781.1..1,855.2) | 1,714.1 (1,664.6..1,736.2) |

### rust-oinf vs rust-o0

30 faster, 61 ties, 9 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | rust-o0 | rust-oinf |
|---|---|---|---|---|
| P6 A cpu each-batch counters none calls/s | 0.913 | ±7.1% | 112,913.6 (111,132.0..123,108.5) | 103,130.9 (100,722.6..104,342.0) |
| P6 A cpu each-batch counters counters calls/s | 0.908 | ±5.4% | 86,573.4 (81,852.4..87,858.0) | 78,578.9 (77,835.3..80,936.6) |
| P6 B each-batch window 256 calls/s | 0.818 | ±9.9% | 87,745.1 (84,182.9..88,317.6) | 71,735.5 (68,260.5..79,055.7) |
| P6 B each-batch retire_buffer 1024 calls/s | 0.918 | ±6.5% | 80,765.9 (77,869.8..81,077.7) | 74,172.9 (71,468.1..78,183.3) |
| GIL torch / thread engine items/s at 300/s | 0.933 | ±3.8% | 6,909.1 (6,550.3..6,960.4) | 6,448.3 (6,397.6..6,499.5) |
| GIL torch / thread engine items/s at ceiling | 0.727 | ±11.4% | 562.5 (511.4..639.4) | 409.0 (409.0..409.2) |
| GIL torch / process engine items/s at ceiling | 0.309 | ±10.3% | 1,739.1 (1,661.0..2,018.6) | 537.0 (536.8..537.2) |
| GIL python / thread engine items/s at 1000/s | 0.902 | ±7.7% | 389.1 (375.0..409.1) | 351.0 (347.1..370.3) |
| GIL python / thread engine items/s at ceiling | 0.908 | ±5.2% | 392.9 (366.8..397.7) | 356.6 (353.6..362.7) |

### rust-onone vs rust-o0

29 faster, 67 ties, 4 slower, 0 missing.

Every slower shape:

| shape | ratio | spread | rust-o0 | rust-onone |
|---|---|---|---|---|
| P6 B each-batch window 256 calls/s | 0.865 | ±11.2% | 87,745.1 (84,182.9..88,317.6) | 75,892.1 (69,633.1..83,108.8) |
| GIL torch / thread engine items/s at 1000/s | 0.890 | ±10.7% | 4,195.0 (3,811.9..4,478.0) | 3,735.3 (3,530.9..3,735.7) |
| GIL torch / thread engine items/s at ceiling | 0.727 | ±14.5% | 562.5 (511.4..639.4) | 408.6 (383.6..409.1) |
| GIL torch / process engine items/s at ceiling | 0.323 | ±14.8% | 1,739.1 (1,661.0..2,018.6) | 562.5 (511.7..562.7) |

### Every shape against python

Ratio (above 1 is faster) and verdict: + faster, = tie, − slower, ? missing.

| shape | rust-o0 | rust-o20 | rust-o100 | rust-o300 | rust-o1000 | rust-o5000 | rust-oinf | rust-onone |
|---|---|---|---|---|---|---|---|---|
| ceiling chain-inline (default) calls/s | 3.82 + | 3.84 + | 4.45 + | 4.96 + | 4.89 + | 5.19 + | 5.12 + | 5.11 + |
| ceiling chain-inline (process) calls/s | 3.93 + | 4.23 + | 4.62 + | 4.93 + | 5.16 + | 5.43 + | 5.15 + | 5.38 + |
| ceiling chain-call (default) calls/s | 1.75 + | 1.84 + | 1.82 + | 1.83 + | 1.76 + | 1.86 + | 1.83 + | 1.87 + |
| ceiling chain-call (process) calls/s | 1.88 + | 1.88 + | 1.94 + | 1.88 + | 1.88 + | 1.93 + | 1.87 + | 1.92 + |
| ceiling each-batch (default) calls/s | 2.85 + | 2.95 + | 2.93 + | 2.90 + | 2.82 + | 2.65 + | 2.62 + | 2.77 + |
| ceiling each-batch (process) calls/s | 2.63 + | 2.54 + | 2.64 + | 2.62 + | 2.54 + | 2.50 + | 2.54 + | 2.50 + |
| scaling resident 500 us | 1.21 = | 1.10 = | 1.51 + | 1.52 + | 1.40 = | 1.60 + | 1.56 + | 1.53 + |
| scaling resident 2000 us | 1.27 + | 1.27 + | 1.39 + | 1.61 + | 1.53 + | 1.61 + | 1.57 + | 1.32 + |
| scaling resident 8000 us | 1.09 = | 1.19 + | 1.23 + | 1.42 + | 1.37 + | 1.45 + | 1.38 + | 1.34 + |
| scaling each 250 us | 1.45 = | 1.74 + | 1.96 + | 2.48 + | 2.06 + | 2.71 + | 2.63 + | 2.39 + |
| scaling each 1000 us | 1.26 = | 1.91 + | 1.92 + | 2.64 + | 2.44 + | 2.74 + | 2.58 + | 2.11 + |
| scaling each 4000 us | 1.32 + | 2.03 + | 2.21 + | 2.55 + | 2.73 + | 2.80 + | 2.69 + | 2.52 + |
| P6 A cpu chain-inline all none calls/s | 3.11 + | 3.03 + | 3.25 + | 3.54 + | 3.66 + | 3.59 + | 3.83 + | 3.63 + |
| P6 A cpu chain-inline all counters calls/s | 2.63 + | 2.75 + | 2.72 + | 2.93 + | 3.06 + | 3.22 + | 2.93 + | 3.26 + |
| P6 A cpu chain-inline all report calls/s | 0.80 − | 0.78 − | 0.96 = | 1.33 + | 1.35 + | 1.41 + | 1.45 + | 1.45 + |
| P6 A cpu chain-inline sampled none calls/s | 3.46 + | 3.16 + | 3.65 + | 3.52 + | 4.03 + | 3.95 + | 4.02 + | 3.71 + |
| P6 A cpu chain-inline sampled counters calls/s | 2.68 + | 2.55 + | 2.69 + | 2.82 + | 2.81 + | 3.03 + | 3.07 + | 3.14 + |
| P6 A cpu chain-inline sampled report calls/s | 1.18 + | 1.13 + | 1.25 + | 1.70 + | 2.00 + | 2.07 + | 2.17 + | 2.06 + |
| P6 A cpu chain-inline counters none calls/s | 3.40 + | 3.34 + | 3.36 + | 3.89 + | 3.69 + | 4.07 + | 3.95 + | 4.12 + |
| P6 A cpu chain-inline counters counters calls/s | 2.88 + | 2.85 + | 2.97 + | 3.15 + | 3.30 + | 3.30 + | 3.24 + | 3.23 + |
| P6 A cpu chain-inline counters report calls/s | 1.59 + | 1.55 + | 1.87 + | 2.11 + | 2.53 + | 2.41 + | 2.48 + | 2.50 + |
| P6 A cpu chain-call all none calls/s | 1.84 + | 1.75 + | 1.81 + | 1.81 + | 1.73 + | 1.73 + | 1.82 + | 1.85 + |
| P6 A cpu chain-call all counters calls/s | 1.45 + | 1.51 + | 1.53 + | 1.57 + | 1.52 + | 1.52 + | 1.64 + | 1.56 + |
| P6 A cpu chain-call all report calls/s | 1.26 + | 1.21 + | 1.15 + | 1.15 + | 1.26 + | 1.24 + | 1.38 + | 1.32 + |
| P6 A cpu chain-call sampled none calls/s | 1.83 + | 1.81 + | 1.79 + | 1.83 + | 1.79 + | 1.74 + | 1.81 + | 1.86 + |
| P6 A cpu chain-call sampled counters calls/s | 1.58 + | 1.55 + | 1.48 + | 1.50 + | 1.51 + | 1.53 + | 1.60 + | 1.56 + |
| P6 A cpu chain-call sampled report calls/s | 1.31 + | 1.32 + | 1.24 + | 1.25 + | 1.29 + | 1.28 + | 1.37 + | 1.34 + |
| P6 A cpu chain-call counters none calls/s | 1.81 + | 1.72 + | 1.66 + | 1.71 + | 1.67 + | 1.85 + | 1.91 + | 1.83 + |
| P6 A cpu chain-call counters counters calls/s | 1.57 + | 1.46 + | 1.51 + | 1.51 + | 1.49 + | 1.57 + | 1.52 + | 1.60 + |
| P6 A cpu chain-call counters report calls/s | 1.43 + | 1.40 + | 1.39 + | 1.42 + | 1.43 + | 1.44 + | 1.48 + | 1.47 + |
| P6 A cpu each-batch all none calls/s | 2.82 + | 2.65 + | 2.81 + | 2.58 + | 2.60 + | 2.65 + | 2.60 + | 2.66 + |
| P6 A cpu each-batch all counters calls/s | 2.38 + | 2.36 + | 2.26 + | 2.24 + | 2.30 + | 2.23 + | 2.32 + | 2.35 + |
| P6 A cpu each-batch all report calls/s | 1.38 + | 1.42 + | 1.37 + | 1.41 + | 1.31 + | 1.44 + | 1.43 + | 1.50 + |
| P6 A cpu each-batch sampled none calls/s | 2.65 + | 2.61 + | 2.67 + | 2.59 + | 2.57 + | 2.40 + | 2.66 + | 2.70 + |
| P6 A cpu each-batch sampled counters calls/s | 2.29 + | 2.25 + | 2.25 + | 2.31 + | 2.38 + | 2.17 + | 2.25 + | 2.25 + |
| P6 A cpu each-batch sampled report calls/s | 1.72 + | 1.64 + | 1.73 + | 1.76 + | 1.86 + | 1.71 + | 1.83 + | 1.76 + |
| P6 A cpu each-batch counters none calls/s | 2.82 + | 2.57 + | 3.02 + | 2.94 + | 2.67 + | 2.67 + | 2.58 + | 2.64 + |
| P6 A cpu each-batch counters counters calls/s | 2.41 + | 2.33 + | 2.29 + | 2.34 + | 2.24 + | 2.08 + | 2.18 + | 2.29 + |
| P6 A cpu each-batch counters report calls/s | 2.18 + | 2.16 + | 2.11 + | 2.10 + | 2.16 + | 1.96 + | 2.14 + | 2.11 + |
| P6 A mixed all none calls/s | 1.31 + | 1.32 + | 1.21 + | 1.18 + | 1.24 + | 1.23 + | 1.34 + | 1.37 + |
| P6 A mixed all counters calls/s | 1.27 + | 1.28 + | 1.14 = | 1.17 = | 1.16 = | 1.13 = | 1.29 + | 1.35 + |
| P6 A mixed all report calls/s | 1.18 + | 1.23 + | 1.10 + | 1.14 + | 1.10 = | 1.17 + | 1.32 + | 1.29 + |
| P6 A mixed sampled none calls/s | 1.37 + | 1.38 + | 1.22 + | 1.37 + | 1.30 + | 1.25 + | 1.50 + | 1.50 + |
| P6 A mixed sampled counters calls/s | 1.30 + | 1.40 + | 1.25 + | 1.32 + | 1.22 = | 1.18 = | 1.46 + | 1.45 + |
| P6 A mixed sampled report calls/s | 1.24 + | 1.18 = | 1.16 = | 1.18 = | 1.15 = | 1.15 = | 1.38 + | 1.34 + |
| P6 A mixed counters none calls/s | 1.30 = | 1.32 + | 1.25 + | 1.28 + | 1.22 = | 1.24 = | 1.41 + | 1.36 + |
| P6 A mixed counters counters calls/s | 1.23 + | 1.31 + | 1.15 + | 1.22 + | 1.23 + | 1.18 = | 1.36 + | 1.36 + |
| P6 A mixed counters report calls/s | 1.27 + | 1.32 + | 1.20 + | 1.23 + | 1.20 = | 1.19 = | 1.28 + | 1.39 + |
| P6 A mixed example-default calls/s | 1.28 + | 1.32 + | 1.25 + | 1.26 + | 1.27 + | 1.26 + | 1.36 + | 1.36 + |
| P6 B chain-inline defaults calls/s | 2.65 + | 2.52 + | 3.10 + | 3.18 + | 3.23 + | 3.22 + | 3.34 + | 3.19 + |
| P6 B chain-inline window 16 calls/s | 2.57 + | 2.66 + | 3.01 + | 3.09 + | 3.42 + | 3.26 + | 3.23 + | 3.24 + |
| P6 B chain-inline window 256 calls/s | 2.47 + | 2.49 + | 3.14 + | 3.04 + | 3.35 + | 3.36 + | 3.38 + | 3.15 + |
| P6 B chain-inline retire_buffer 1024 calls/s | 2.54 + | 2.75 + | 3.13 + | 3.05 + | 3.28 + | 3.47 + | 3.37 + | 3.27 + |
| P6 B chain-inline event_sample 0.0 calls/s | 2.75 + | 2.79 + | 3.23 + | 3.23 + | 3.37 + | 3.54 + | 3.44 + | 3.29 + |
| P6 B chain-inline event_sample 1.0 calls/s | 2.47 + | 2.68 + | 3.06 + | 3.10 + | 3.32 + | 3.25 + | 3.17 + | 3.29 + |
| P6 B chain-inline Fixed(16) calls/s | 1.91 + | 2.87 + | 3.72 + | 3.79 + | 3.75 + | 3.77 + | 3.75 + | 3.69 + |
| P6 B chain-inline Fixed(256) calls/s | 2.56 + | 2.61 + | 2.82 + | 2.92 + | 3.00 + | 3.00 + | 2.97 + | 2.88 + |
| P6 B chain-call defaults calls/s | 1.41 + | 1.41 + | 1.52 + | 1.44 + | 1.50 + | 1.49 + | 1.43 + | 1.46 + |
| P6 B chain-call window 16 calls/s | 1.41 + | 1.42 + | 1.54 + | 1.49 + | 1.50 + | 1.49 + | 1.44 + | 1.45 + |
| P6 B chain-call window 256 calls/s | 1.42 + | 1.40 + | 1.47 + | 1.45 + | 1.44 + | 1.48 + | 1.48 + | 1.43 + |
| P6 B chain-call retire_buffer 1024 calls/s | 1.47 + | 1.48 + | 1.55 + | 1.52 + | 1.51 + | 1.53 + | 1.51 + | 1.53 + |
| P6 B chain-call event_sample 0.0 calls/s | 1.37 + | 1.38 + | 1.43 + | 1.43 + | 1.35 + | 1.45 + | 1.44 + | 1.47 + |
| P6 B chain-call event_sample 1.0 calls/s | 1.40 + | 1.35 + | 1.41 + | 1.47 + | 1.39 + | 1.50 + | 1.50 + | 1.42 + |
| P6 B chain-call Fixed(16) calls/s | 1.30 + | 1.23 + | 1.37 + | 1.46 + | 1.41 + | 1.49 + | 1.47 + | 1.38 + |
| P6 B chain-call Fixed(256) calls/s | 1.41 + | 1.37 + | 1.39 + | 1.43 + | 1.51 + | 1.45 + | 1.45 + | 1.48 + |
| P6 B chain-call TargetLoad calls/s | 1.38 + | 1.34 + | 1.39 + | 1.47 + | 1.50 + | 1.46 + | 1.43 + | 1.44 + |
| P6 B each-batch defaults calls/s | 2.29 + | 2.38 + | 2.45 + | 2.42 + | 2.60 + | 2.11 + | 2.23 + | 2.19 + |
| P6 B each-batch window 16 calls/s | 2.07 + | 2.19 + | 2.29 + | 2.40 + | 2.31 + | 2.26 + | 2.24 + | 2.14 + |
| P6 B each-batch window 256 calls/s | 2.35 + | 2.37 + | 2.35 + | 2.18 + | 2.08 + | 2.16 + | 1.92 + | 2.03 + |
| P6 B each-batch retire_buffer 1024 calls/s | 2.27 + | 2.38 + | 2.33 + | 2.46 + | 2.36 + | 2.24 + | 2.09 + | 2.21 + |
| P6 B each-batch event_sample 0.0 calls/s | 2.35 + | 2.45 + | 2.37 + | 2.47 + | 2.45 + | 2.22 + | 2.17 + | 2.26 + |
| P6 B each-batch event_sample 1.0 calls/s | 2.30 + | 2.29 + | 2.37 + | 2.52 + | 2.44 + | 2.21 + | 2.07 + | 2.24 + |
| P6 B each-batch Fixed(16) calls/s | 2.28 + | 2.26 + | 2.34 + | 2.25 + | 2.40 + | 2.24 + | 2.15 + | 2.21 + |
| P6 B each-batch Fixed(256) calls/s | 2.08 + | 2.15 + | 2.15 + | 2.28 + | 2.25 + | 2.05 + | 2.20 + | 2.10 + |
| P6 B each-batch TargetLoad calls/s | 2.24 + | 2.32 + | 2.25 + | 2.25 + | 2.33 + | 2.14 + | 2.01 + | 2.15 + |
| P6 B trace_replay defaults calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = |
| P6 B trace_replay window 16 calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.01 = |
| P6 B trace_replay window 256 calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.01 = | 1.00 = |
| P6 B trace_replay retire_buffer 1024 calls/s | 1.00 = | 1.01 = | 1.00 = | 1.00 = | 1.01 = | 1.01 = | 1.00 = | 1.00 = |
| P6 B trace_replay event_sample 0.0 calls/s | 1.00 = | 1.00 = | 1.01 = | 1.00 = | 1.00 = | 1.00 = | 1.01 = | 1.00 = |
| P6 B trace_replay event_sample 1.0 calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = |
| P6 B trace_replay Fixed(16) calls/s | 1.01 = | 1.00 = | 1.01 = | 1.01 = | 1.01 = | 1.01 = | 1.01 = | 1.00 = |
| P6 B trace_replay Fixed(256) calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.01 = |
| P6 B trace_replay TargetLoad calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = |
| GIL torch / thread engine items/s at 100/s | 1.09 = | 1.09 = | 1.14 = | 1.05 = | 1.09 = | 1.08 = | 1.08 = | 1.09 = |
| GIL torch / thread engine items/s at 300/s | 1.28 + | 1.18 + | 1.22 + | 1.16 + | 1.20 + | 1.24 + | 1.19 + | 1.21 + |
| GIL torch / thread engine items/s at 1000/s | 5.47 + | 4.83 + | 5.00 + | 4.27 + | 5.00 + | 5.13 + | 4.83 + | 4.87 + |
| GIL torch / thread engine items/s at ceiling | 1.83 + | 1.67 + | 1.75 + | 1.58 + | 1.25 + | 1.33 + | 1.33 + | 1.33 + |
| GIL torch / process engine items/s at 100/s | 1.00 = | 1.00 = | 0.98 = | 1.01 = | 0.98 = | 0.99 = | 0.98 = | 0.99 = |
| GIL torch / process engine items/s at 300/s | 0.99 = | 0.99 = | 0.97 = | 0.98 = | 0.99 = | 0.98 = | 0.99 = | 0.99 = |
| GIL torch / process engine items/s at 1000/s | 7.87 + | 7.78 + | 7.87 + | 7.81 + | 7.61 + | 7.72 + | 7.75 + | 7.69 + |
| GIL torch / process engine items/s at ceiling | 3.40 + | 3.75 + | 3.95 + | 3.85 + | 1.05 = | 1.10 + | 1.05 + | 1.10 + |
| GIL python / thread engine items/s at 100/s | 1.01 = | 1.02 = | 1.03 = | 1.00 = | 1.04 = | 1.00 = | 0.94 − | 1.05 = |
| GIL python / thread engine items/s at 300/s | 1.02 = | 1.04 = | 1.06 = | 1.01 = | 1.04 = | 0.97 = | 0.92 = | 1.02 = |
| GIL python / thread engine items/s at 1000/s | 1.00 = | 1.03 = | 1.05 = | 0.99 = | 1.03 = | 0.95 = | 0.90 − | 1.01 = |
| GIL python / thread engine items/s at ceiling | 1.01 = | 1.05 = | 1.02 = | 0.96 = | 1.02 = | 0.99 = | 0.92 − | 1.04 = |
| GIL python / process engine items/s at 100/s | 1.01 = | 1.00 = | 1.02 = | 1.00 = | 1.06 + | 1.00 = | 1.05 = | 1.05 + |
| GIL python / process engine items/s at 300/s | 1.06 = | 1.05 = | 1.06 = | 1.06 = | 1.09 + | 1.09 + | 1.10 + | 1.07 + |
| GIL python / process engine items/s at 1000/s | 1.28 + | 1.35 + | 1.35 + | 1.28 + | 1.34 + | 1.35 + | 1.43 + | 1.35 + |
| GIL python / process engine items/s at ceiling | 1.34 + | 1.38 + | 1.36 + | 1.25 + | 1.27 + | 1.28 + | 1.37 + | 1.29 + |

### Every shape against rust-o0

Ratio (above 1 is faster) and verdict: + faster, = tie, − slower, ? missing.

| shape | rust-o20 | rust-o100 | rust-o300 | rust-o1000 | rust-o5000 | rust-oinf | rust-onone |
|---|---|---|---|---|---|---|---|
| ceiling chain-inline (default) calls/s | 1.01 = | 1.17 = | 1.30 + | 1.28 + | 1.36 + | 1.34 + | 1.34 + |
| ceiling chain-inline (process) calls/s | 1.08 = | 1.18 = | 1.26 + | 1.31 + | 1.38 + | 1.31 + | 1.37 + |
| ceiling chain-call (default) calls/s | 1.05 = | 1.04 = | 1.04 = | 1.00 = | 1.06 + | 1.04 = | 1.07 = |
| ceiling chain-call (process) calls/s | 1.00 = | 1.03 = | 1.00 = | 1.00 = | 1.03 = | 0.99 = | 1.02 = |
| ceiling each-batch (default) calls/s | 1.04 = | 1.03 = | 1.02 = | 0.99 = | 0.93 = | 0.92 = | 0.97 = |
| ceiling each-batch (process) calls/s | 0.97 = | 1.00 = | 0.99 = | 0.97 = | 0.95 = | 0.97 = | 0.95 = |
| scaling resident 500 us | 0.91 = | 1.25 = | 1.26 = | 1.16 = | 1.32 + | 1.29 + | 1.26 = |
| scaling resident 2000 us | 1.00 = | 1.09 = | 1.27 + | 1.20 + | 1.26 + | 1.23 + | 1.04 = |
| scaling resident 8000 us | 1.10 = | 1.13 = | 1.30 + | 1.26 + | 1.34 + | 1.27 + | 1.23 + |
| scaling each 250 us | 1.20 = | 1.35 = | 1.70 + | 1.42 = | 1.87 + | 1.81 + | 1.64 = |
| scaling each 1000 us | 1.51 + | 1.52 = | 2.09 + | 1.93 + | 2.17 + | 2.04 + | 1.67 + |
| scaling each 4000 us | 1.54 + | 1.68 + | 1.93 + | 2.08 + | 2.13 + | 2.05 + | 1.92 + |
| P6 A cpu chain-inline all none calls/s | 0.97 = | 1.04 = | 1.14 + | 1.18 + | 1.15 + | 1.23 + | 1.17 + |
| P6 A cpu chain-inline all counters calls/s | 1.04 = | 1.03 = | 1.11 + | 1.16 + | 1.23 + | 1.11 + | 1.24 + |
| P6 A cpu chain-inline all report calls/s | 0.97 = | 1.19 = | 1.65 + | 1.68 + | 1.76 + | 1.81 + | 1.81 + |
| P6 A cpu chain-inline sampled none calls/s | 0.91 − | 1.05 = | 1.02 = | 1.16 + | 1.14 + | 1.16 + | 1.07 + |
| P6 A cpu chain-inline sampled counters calls/s | 0.95 = | 1.00 = | 1.05 = | 1.05 = | 1.13 + | 1.15 + | 1.17 + |
| P6 A cpu chain-inline sampled report calls/s | 0.96 = | 1.06 = | 1.44 + | 1.70 + | 1.76 + | 1.84 + | 1.75 + |
| P6 A cpu chain-inline counters none calls/s | 0.98 = | 0.99 = | 1.14 + | 1.08 = | 1.20 + | 1.16 + | 1.21 + |
| P6 A cpu chain-inline counters counters calls/s | 0.99 = | 1.03 = | 1.09 + | 1.15 + | 1.15 + | 1.13 + | 1.12 + |
| P6 A cpu chain-inline counters report calls/s | 0.98 = | 1.18 + | 1.33 + | 1.59 + | 1.52 + | 1.56 + | 1.57 + |
| P6 A cpu chain-call all none calls/s | 0.95 = | 0.98 = | 0.98 = | 0.94 = | 0.94 = | 0.99 = | 1.01 = |
| P6 A cpu chain-call all counters calls/s | 1.04 = | 1.05 = | 1.08 = | 1.04 = | 1.04 = | 1.13 + | 1.08 + |
| P6 A cpu chain-call all report calls/s | 0.96 = | 0.91 = | 0.91 = | 0.99 = | 0.98 = | 1.09 = | 1.05 = |
| P6 A cpu chain-call sampled none calls/s | 0.99 = | 0.98 = | 1.00 = | 0.98 = | 0.95 = | 0.99 = | 1.02 = |
| P6 A cpu chain-call sampled counters calls/s | 0.98 = | 0.94 − | 0.95 = | 0.96 − | 0.97 = | 1.02 = | 0.99 = |
| P6 A cpu chain-call sampled report calls/s | 1.00 = | 0.94 = | 0.95 = | 0.99 = | 0.98 = | 1.04 = | 1.02 = |
| P6 A cpu chain-call counters none calls/s | 0.95 = | 0.92 − | 0.94 = | 0.93 − | 1.02 = | 1.06 = | 1.01 = |
| P6 A cpu chain-call counters counters calls/s | 0.93 = | 0.96 = | 0.96 = | 0.95 = | 1.00 = | 0.97 = | 1.02 = |
| P6 A cpu chain-call counters report calls/s | 0.98 = | 0.97 = | 0.99 = | 1.00 = | 1.01 = | 1.03 = | 1.02 = |
| P6 A cpu each-batch all none calls/s | 0.94 = | 1.00 = | 0.92 = | 0.92 = | 0.94 = | 0.92 = | 0.94 = |
| P6 A cpu each-batch all counters calls/s | 0.99 = | 0.95 = | 0.94 = | 0.97 = | 0.94 = | 0.98 = | 0.99 = |
| P6 A cpu each-batch all report calls/s | 1.02 = | 0.99 = | 1.02 = | 0.95 = | 1.04 = | 1.04 = | 1.08 + |
| P6 A cpu each-batch sampled none calls/s | 0.99 = | 1.01 = | 0.98 = | 0.97 = | 0.91 − | 1.00 = | 1.02 = |
| P6 A cpu each-batch sampled counters calls/s | 0.99 = | 0.98 = | 1.01 = | 1.04 = | 0.95 = | 0.98 = | 0.98 = |
| P6 A cpu each-batch sampled report calls/s | 0.95 − | 1.01 = | 1.02 = | 1.08 = | 0.99 = | 1.07 = | 1.02 = |
| P6 A cpu each-batch counters none calls/s | 0.91 = | 1.07 = | 1.04 = | 0.95 = | 0.95 = | 0.91 − | 0.94 = |
| P6 A cpu each-batch counters counters calls/s | 0.97 = | 0.95 = | 0.97 = | 0.93 − | 0.87 − | 0.91 − | 0.95 = |
| P6 A cpu each-batch counters report calls/s | 0.99 = | 0.97 = | 0.96 = | 0.99 = | 0.90 − | 0.98 = | 0.97 = |
| P6 A mixed all none calls/s | 1.01 = | 0.92 = | 0.90 − | 0.94 = | 0.94 = | 1.02 = | 1.05 = |
| P6 A mixed all counters calls/s | 1.01 = | 0.90 = | 0.92 = | 0.91 = | 0.89 = | 1.02 = | 1.06 = |
| P6 A mixed all report calls/s | 1.04 = | 0.93 = | 0.97 = | 0.93 = | 0.99 = | 1.11 = | 1.09 + |
| P6 A mixed sampled none calls/s | 1.01 = | 0.90 = | 1.01 = | 0.95 = | 0.91 = | 1.09 = | 1.10 + |
| P6 A mixed sampled counters calls/s | 1.08 = | 0.97 = | 1.02 = | 0.94 = | 0.91 = | 1.13 = | 1.12 + |
| P6 A mixed sampled report calls/s | 0.95 = | 0.94 = | 0.95 = | 0.92 = | 0.92 = | 1.11 = | 1.07 = |
| P6 A mixed counters none calls/s | 1.02 = | 0.96 = | 0.98 = | 0.94 = | 0.95 = | 1.09 = | 1.04 = |
| P6 A mixed counters counters calls/s | 1.07 = | 0.94 = | 1.00 = | 1.00 = | 0.96 = | 1.11 = | 1.11 = |
| P6 A mixed counters report calls/s | 1.03 = | 0.94 = | 0.97 = | 0.94 = | 0.93 = | 1.01 = | 1.10 = |
| P6 A mixed example-default calls/s | 1.03 = | 0.98 = | 0.98 = | 0.99 = | 0.98 = | 1.07 = | 1.06 + |
| P6 B chain-inline defaults calls/s | 0.95 = | 1.17 + | 1.20 + | 1.22 + | 1.22 + | 1.26 + | 1.20 + |
| P6 B chain-inline window 16 calls/s | 1.03 = | 1.17 + | 1.20 + | 1.33 + | 1.27 + | 1.26 + | 1.26 + |
| P6 B chain-inline window 256 calls/s | 1.01 = | 1.27 + | 1.23 + | 1.36 + | 1.36 + | 1.37 + | 1.27 + |
| P6 B chain-inline retire_buffer 1024 calls/s | 1.09 = | 1.23 + | 1.20 + | 1.30 + | 1.37 + | 1.33 + | 1.29 + |
| P6 B chain-inline event_sample 0.0 calls/s | 1.01 = | 1.18 + | 1.17 = | 1.23 + | 1.29 + | 1.25 + | 1.20 + |
| P6 B chain-inline event_sample 1.0 calls/s | 1.09 = | 1.24 + | 1.26 + | 1.34 + | 1.31 + | 1.28 + | 1.33 + |
| P6 B chain-inline Fixed(16) calls/s | 1.50 + | 1.95 + | 1.98 + | 1.96 + | 1.97 + | 1.96 + | 1.93 + |
| P6 B chain-inline Fixed(256) calls/s | 1.02 = | 1.10 = | 1.14 + | 1.17 + | 1.17 + | 1.16 + | 1.12 + |
| P6 B chain-call defaults calls/s | 1.00 = | 1.07 + | 1.02 = | 1.06 + | 1.06 + | 1.01 = | 1.03 = |
| P6 B chain-call window 16 calls/s | 1.01 = | 1.09 = | 1.06 + | 1.07 = | 1.06 + | 1.02 = | 1.03 = |
| P6 B chain-call window 256 calls/s | 0.98 = | 1.03 = | 1.02 = | 1.01 = | 1.04 + | 1.04 + | 1.01 = |
| P6 B chain-call retire_buffer 1024 calls/s | 1.00 = | 1.05 = | 1.03 = | 1.03 = | 1.04 = | 1.03 = | 1.04 = |
| P6 B chain-call event_sample 0.0 calls/s | 1.01 = | 1.04 = | 1.04 = | 0.98 = | 1.06 = | 1.05 = | 1.07 = |
| P6 B chain-call event_sample 1.0 calls/s | 0.97 = | 1.01 = | 1.05 = | 1.00 = | 1.07 = | 1.07 + | 1.01 = |
| P6 B chain-call Fixed(16) calls/s | 0.95 = | 1.05 = | 1.13 + | 1.09 = | 1.15 + | 1.13 = | 1.06 = |
| P6 B chain-call Fixed(256) calls/s | 0.98 = | 0.99 = | 1.02 = | 1.07 + | 1.03 = | 1.03 = | 1.05 + |
| P6 B chain-call TargetLoad calls/s | 0.98 = | 1.01 = | 1.06 = | 1.09 + | 1.06 = | 1.04 = | 1.05 = |
| P6 B each-batch defaults calls/s | 1.04 = | 1.07 + | 1.06 = | 1.14 + | 0.92 − | 0.97 = | 0.95 = |
| P6 B each-batch window 16 calls/s | 1.06 = | 1.10 = | 1.16 + | 1.11 + | 1.09 = | 1.08 + | 1.03 = |
| P6 B each-batch window 256 calls/s | 1.01 = | 1.00 = | 0.93 = | 0.89 − | 0.92 − | 0.82 − | 0.86 − |
| P6 B each-batch retire_buffer 1024 calls/s | 1.05 = | 1.03 = | 1.08 + | 1.04 = | 0.98 = | 0.92 − | 0.97 = |
| P6 B each-batch event_sample 0.0 calls/s | 1.05 = | 1.01 = | 1.05 = | 1.04 = | 0.95 = | 0.93 = | 0.96 = |
| P6 B each-batch event_sample 1.0 calls/s | 0.99 = | 1.03 = | 1.09 = | 1.06 = | 0.96 = | 0.90 = | 0.97 = |
| P6 B each-batch Fixed(16) calls/s | 0.99 = | 1.03 = | 0.99 = | 1.05 = | 0.99 = | 0.94 = | 0.97 = |
| P6 B each-batch Fixed(256) calls/s | 1.03 = | 1.03 = | 1.10 = | 1.08 = | 0.98 = | 1.06 = | 1.01 = |
| P6 B each-batch TargetLoad calls/s | 1.03 = | 1.00 = | 1.00 = | 1.04 = | 0.95 − | 0.90 = | 0.96 = |
| P6 B trace_replay defaults calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = |
| P6 B trace_replay window 16 calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.01 = | 1.00 = | 1.01 = |
| P6 B trace_replay window 256 calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 0.99 = | 1.00 = | 1.00 = |
| P6 B trace_replay retire_buffer 1024 calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = |
| P6 B trace_replay event_sample 0.0 calls/s | 1.00 = | 1.01 = | 1.00 = | 1.00 = | 1.00 = | 1.01 = | 1.00 = |
| P6 B trace_replay event_sample 1.0 calls/s | 1.00 = | 0.99 = | 1.00 = | 1.00 = | 1.00 = | 0.99 = | 0.99 = |
| P6 B trace_replay Fixed(16) calls/s | 0.99 = | 1.00 = | 1.00 = | 0.99 = | 0.99 = | 0.99 = | 0.99 = |
| P6 B trace_replay Fixed(256) calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 1.01 = |
| P6 B trace_replay TargetLoad calls/s | 1.00 = | 1.00 = | 1.00 = | 1.00 = | 0.99 = | 1.00 = | 1.00 = |
| GIL torch / thread engine items/s at 100/s | 1.00 = | 1.05 = | 0.97 = | 1.00 = | 0.99 = | 0.99 = | 1.00 = |
| GIL torch / thread engine items/s at 300/s | 0.92 = | 0.95 = | 0.91 = | 0.94 = | 0.97 = | 0.93 − | 0.94 = |
| GIL torch / thread engine items/s at 1000/s | 0.88 = | 0.91 = | 0.78 = | 0.91 = | 0.94 = | 0.88 = | 0.89 − |
| GIL torch / thread engine items/s at ceiling | 0.91 = | 0.96 = | 0.86 = | 0.68 − | 0.73 − | 0.73 − | 0.73 − |
| GIL torch / process engine items/s at 100/s | 1.01 = | 0.99 = | 1.02 = | 0.98 = | 1.00 = | 0.99 = | 0.99 = |
| GIL torch / process engine items/s at 300/s | 0.99 = | 0.98 = | 0.98 = | 1.00 = | 0.99 = | 0.99 = | 1.00 = |
| GIL torch / process engine items/s at 1000/s | 0.99 = | 1.00 = | 0.99 = | 0.97 = | 0.98 = | 0.99 = | 0.98 = |
| GIL torch / process engine items/s at ceiling | 1.10 = | 1.16 = | 1.13 = | 0.31 − | 0.32 − | 0.31 − | 0.32 − |
| GIL python / thread engine items/s at 100/s | 1.02 = | 1.02 = | 0.99 = | 1.03 = | 1.00 = | 0.93 = | 1.04 = |
| GIL python / thread engine items/s at 300/s | 1.03 = | 1.04 = | 0.99 = | 1.03 = | 0.95 = | 0.90 = | 1.01 = |
| GIL python / thread engine items/s at 1000/s | 1.03 = | 1.05 = | 0.99 = | 1.03 = | 0.95 = | 0.90 − | 1.01 = |
| GIL python / thread engine items/s at ceiling | 1.04 = | 1.01 = | 0.95 = | 1.01 = | 0.98 = | 0.91 − | 1.03 = |
| GIL python / process engine items/s at 100/s | 0.99 = | 1.01 = | 0.99 = | 1.04 + | 0.99 = | 1.03 = | 1.03 = |
| GIL python / process engine items/s at 300/s | 0.99 = | 1.00 = | 1.00 = | 1.03 = | 1.03 = | 1.03 = | 1.01 = |
| GIL python / process engine items/s at 1000/s | 1.05 = | 1.05 = | 1.00 = | 1.04 = | 1.05 = | 1.11 + | 1.05 = |
| GIL python / process engine items/s at ceiling | 1.03 = | 1.01 = | 0.93 − | 0.95 − | 0.95 − | 1.02 = | 0.96 = |

## Dispatcher ceiling

Calls/s, median (min..max) of 5 interleaved rounds; us/call from the median.

| shape | setting | python calls/s | rust-o0 calls/s | rust-o20 calls/s | rust-o100 calls/s | rust-o300 calls/s | rust-o1000 calls/s | rust-o5000 calls/s | rust-oinf calls/s | rust-onone calls/s | python us/call | rust-o0 us/call | rust-o20 us/call | rust-o100 us/call | rust-o300 us/call | rust-o1000 us/call | rust-o5000 us/call | rust-oinf us/call | rust-onone us/call | rust-o0/python | rust-o20/python | rust-o100/python | rust-o300/python | rust-o1000/python | rust-o5000/python | rust-oinf/python | rust-onone/python |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| chain-inline | default | 84,445 (80,135..86,840) | 322,227 (290,349..340,027) | 324,033 (283,070..356,831) | 375,610 (337,157..415,281) | 418,777 (355,651..435,685) | 412,653 (343,728..439,949) | 437,899 (372,253..443,522) | 432,588 (374,015..450,895) | 431,650 (384,436..449,521) | 11.8 | 3.1 | 3.1 | 2.7 | 2.4 | 2.4 | 2.3 | 2.3 | 2.3 | 3.82x | 3.84x | 4.45x | 4.96x | 4.89x | 5.19x | 5.12x | 5.11x |
| chain-inline | process | 78,331 (77,015..81,432) | 307,569 (240,707..328,913) | 331,244 (294,819..345,289) | 361,877 (321,708..403,881) | 386,170 (341,344..403,890) | 403,957 (353,649..432,063) | 425,131 (350,760..438,026) | 403,218 (323,454..433,167) | 421,522 (328,109..439,771) | 12.8 | 3.3 | 3.0 | 2.8 | 2.6 | 2.5 | 2.4 | 2.5 | 2.4 | 3.93x | 4.23x | 4.62x | 4.93x | 5.16x | 5.43x | 5.15x | 5.38x |
| chain-call | default | 30,588 (30,151..31,342) | 53,667 (51,507..55,218) | 56,226 (53,710..57,778) | 55,804 (53,588..57,839) | 55,926 (52,887..56,470) | 53,902 (53,632..58,496) | 56,913 (56,405..58,613) | 55,867 (54,428..57,619) | 57,184 (51,269..57,824) | 32.7 | 18.6 | 17.8 | 17.9 | 17.9 | 18.6 | 17.6 | 17.9 | 17.5 | 1.75x | 1.84x | 1.82x | 1.83x | 1.76x | 1.86x | 1.83x | 1.87x |
| chain-call | process | 29,107 (28,295..29,310) | 54,704 (50,436..56,943) | 54,782 (53,787..56,017) | 56,497 (54,845..59,481) | 54,813 (53,629..57,569) | 54,613 (53,540..57,688) | 56,109 (52,959..59,125) | 54,419 (53,562..59,256) | 55,831 (53,631..58,343) | 34.4 | 18.3 | 18.3 | 17.7 | 18.2 | 18.3 | 17.8 | 18.4 | 17.9 | 1.88x | 1.88x | 1.94x | 1.88x | 1.88x | 1.93x | 1.87x | 1.92x |
| each-batch | default | 42,507 (42,160..43,825) | 120,980 (116,371..126,391) | 125,521 (119,436..126,705) | 124,633 (123,630..128,493) | 123,110 (113,730..134,375) | 119,761 (118,984..123,965) | 112,742 (107,650..122,910) | 111,330 (108,281..120,570) | 117,849 (114,037..123,930) | 23.5 | 8.3 | 8.0 | 8.0 | 8.1 | 8.3 | 8.9 | 9.0 | 8.5 | 2.85x | 2.95x | 2.93x | 2.90x | 2.82x | 2.65x | 2.62x | 2.77x |
| each-batch | process | 39,945 (39,073..40,864) | 105,081 (102,073..117,835) | 101,415 (89,955..106,608) | 105,379 (91,567..111,728) | 104,550 (93,168..119,684) | 101,579 (98,423..111,333) | 99,856 (94,035..104,008) | 101,562 (94,763..110,506) | 99,674 (86,215..104,862) | 25.0 | 9.5 | 9.9 | 9.5 | 9.6 | 9.8 | 10.0 | 9.8 | 10.0 | 2.63x | 2.54x | 2.64x | 2.62x | 2.54x | 2.50x | 2.54x | 2.50x |

## Scaling

Us per job (resident) or per item (each), median (min..max) of the measured repeats over every round (a warm-up run discarded first); lower is better.

| shape n | python | rust-o0 | rust-o20 | rust-o100 | rust-o300 | rust-o1000 | rust-o5000 | rust-oinf | rust-onone | rust-o0/python | rust-o20/python | rust-o100/python | rust-o300/python | rust-o1000/python | rust-o5000/python | rust-oinf/python | rust-onone/python |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| resident 500 | 52.0 (49.2..57.0) | 43.0 (34.5..49.1) | 47.3 (36.1..61.8) | 34.4 (31.6..41.8) | 34.2 (31.6..39.8) | 37.2 (34.3..61.1) | 32.5 (31.2..37.1) | 33.3 (31.7..35.2) | 34.1 (31.4..48.5) | 0.83x | 0.91x | 0.66x | 0.66x | 0.71x | 0.62x | 0.64x | 0.66x |
| resident 2000 | 52.7 (51.5..63.9) | 41.4 (39.9..46.3) | 41.5 (36.5..43.2) | 37.9 (35.1..40.6) | 32.7 (31.0..33.9) | 34.5 (33.0..35.8) | 32.8 (31.5..34.3) | 33.6 (32.1..34.4) | 39.8 (35.5..47.5) | 0.79x | 0.79x | 0.72x | 0.62x | 0.65x | 0.62x | 0.64x | 0.75x |
| resident 8000 | 55.5 (53.0..57.1) | 51.0 (42.2..53.3) | 46.6 (42.0..49.1) | 45.1 (42.6..48.7) | 39.2 (38.2..41.4) | 40.6 (39.8..42.2) | 38.2 (37.2..40.6) | 40.3 (36.7..42.2) | 41.5 (39.2..44.2) | 0.92x | 0.84x | 0.81x | 0.71x | 0.73x | 0.69x | 0.73x | 0.75x |
| each 250 | 82.6 (70.1..105.7) | 56.8 (53.0..81.6) | 47.4 (36.6..70.0) | 42.2 (31.3..58.7) | 33.3 (30.0..40.8) | 40.1 (33.8..73.4) | 30.4 (26.1..33.7) | 31.4 (27.7..38.1) | 34.6 (28.7..71.2) | 0.69x | 0.57x | 0.51x | 0.40x | 0.49x | 0.37x | 0.38x | 0.42x |
| each 1000 | 79.5 (73.1..84.7) | 62.9 (55.7..93.1) | 41.5 (36.5..49.7) | 41.4 (26.3..45.8) | 30.1 (26.7..37.9) | 32.6 (30.7..37.7) | 29.0 (26.9..35.3) | 30.8 (25.0..32.9) | 37.6 (29.9..42.3) | 0.79x | 0.52x | 0.52x | 0.38x | 0.41x | 0.36x | 0.39x | 0.47x |
| each 4000 | 78.7 (75.5..82.5) | 59.9 (51.4..66.0) | 38.7 (32.9..42.6) | 35.7 (31.1..42.1) | 30.9 (27.2..35.5) | 28.8 (25.6..34.6) | 28.1 (26.7..30.5) | 29.2 (27.9..31.9) | 31.2 (30.7..33.2) | 0.76x | 0.49x | 0.45x | 0.39x | 0.37x | 0.36x | 0.37x | 0.40x |

Regions and GIL offers of the Rust variants per job or item: offers (released regions / held regions), and offers/s, medians.

| shape n | rust-o0 | rust-o20 | rust-o100 | rust-o300 | rust-o1000 | rust-o5000 | rust-oinf | rust-onone |
|---|---|---|---|---|---|---|---|---|
| resident 500 | 1.02 (1.02 / 0.00), 23,740/s | 0.30 (0.30 / 0.72), 6,770/s | 0.08 (0.08 / 0.94), 2,386/s | 0.03 (0.03 / 0.99), 1,001/s | 0.01 (0.01 / 1.01), 133/s | 0.00 (0.00 / 1.02), 62/s | 0.00 (0.00 / 1.02), 0/s | 0.00 (0.00 / 0.00), 0/s |
| resident 2000 | 1.02 (1.02 / 0.00), 24,541/s | 0.34 (0.34 / 0.67), 8,358/s | 0.09 (0.09 / 0.92), 2,545/s | 0.03 (0.03 / 0.98), 1,003/s | 0.01 (0.01 / 1.01), 199/s | 0.00 (0.00 / 1.02), 15/s | 0.00 (0.00 / 1.02), 0/s | 0.00 (0.00 / 0.00), 0/s |
| resident 8000 | 1.02 (1.02 / 0.00), 19,903/s | 0.49 (0.49 / 0.52), 10,555/s | 0.15 (0.15 / 0.87), 3,335/s | 0.05 (0.05 / 0.97), 1,254/s | 0.01 (0.01 / 1.00), 360/s | 0.00 (0.00 / 1.02), 3/s | 0.00 (0.00 / 1.02), 0/s | 0.00 (0.00 / 0.00), 0/s |
| each 250 | 2.02 (2.02 / 0.00), 35,739/s | 0.57 (0.54 / 1.51), 12,096/s | 0.15 (0.14 / 1.92), 3,651/s | 0.05 (0.05 / 2.02), 1,613/s | 0.00 (0.00 / 2.08), 0/s | 0.00 (0.00 / 2.08), 0/s | 0.00 (0.00 / 2.08), 0/s | 0.00 (0.00 / 0.00), 0/s |
| each 1000 | 2.01 (2.01 / 0.00), 32,035/s | 0.61 (0.58 / 1.44), 14,721/s | 0.18 (0.16 / 1.89), 4,309/s | 0.05 (0.05 / 2.01), 1,713/s | 0.00 (0.00 / 2.07), 0/s | 0.00 (0.00 / 2.07), 0/s | 0.00 (0.00 / 2.07), 0/s | 0.00 (0.00 / 0.00), 0/s |
| each 4000 | 2.00 (2.00 / 0.00), 33,445/s | 0.60 (0.58 / 1.45), 15,630/s | 0.16 (0.15 / 1.90), 4,695/s | 0.06 (0.05 / 2.00), 1,872/s | 0.00 (0.00 / 2.06), 35/s | 0.00 (0.00 / 2.06), 0/s | 0.00 (0.00 / 2.06), 0/s | 0.00 (0.00 / 0.00), 0/s |

## P6 study A: event levels

Calls/s, median (min..max) over rounds (one round per child, every variant per round); dispatcher us/call median in brackets.

| config | python | rust-o0 | rust-o20 | rust-o100 | rust-o300 | rust-o1000 | rust-o5000 | rust-oinf | rust-onone | rust-o0/python | rust-o20/python | rust-o100/python | rust-o300/python | rust-o1000/python | rust-o5000/python | rust-oinf/python | rust-onone/python |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cpu chain-inline all none | 86,621 (81,316..87,935) [11.4] | 269,663 (251,788..272,216) [3.6] | 262,843 (252,781..264,340) [3.7] | 281,752 (247,380..287,556) [3.4] | 306,296 (306,044..307,904) [3.2] | 317,417 (296,574..333,090) [3.0] | 310,600 (293,488..321,658) [3.1] | 331,842 (327,564..342,751) [2.9] | 314,483 (297,667..334,333) [3.1] | 3.11x | 3.03x | 3.25x | 3.54x | 3.66x | 3.59x | 3.83x | 3.63x |
| cpu chain-inline all counters | 73,106 (69,361..77,177) [13.6] | 192,324 (190,528..210,328) [5.2] | 200,695 (191,359..201,055) [4.9] | 198,996 (195,208..214,652) [4.9] | 214,161 (205,135..230,627) [4.6] | 223,749 (223,542..235,674) [4.4] | 235,682 (216,597..239,618) [4.2] | 214,008 (203,252..227,541) [4.6] | 238,267 (225,789..244,783) [4.1] | 2.63x | 2.75x | 2.72x | 2.93x | 3.06x | 3.22x | 2.93x | 3.26x |
| cpu chain-inline all report | 25,834 (25,816..27,573) [38.5] | 20,706 (20,651..22,362) [48.1] | 20,040 (19,826..23,337) [49.7] | 24,694 (20,216..28,053) [40.3] | 34,248 (33,396..34,285) [29.1] | 34,885 (34,584..37,819) [28.5] | 36,452 (35,571..38,612) [27.3] | 37,466 (36,576..38,382) [26.4] | 37,541 (37,313..37,667) [26.4] | 0.80x | 0.78x | 0.96x | 1.33x | 1.35x | 1.41x | 1.45x | 1.45x |
| cpu chain-inline sampled none | 78,879 (78,206..81,565) [12.6] | 273,273 (267,732..281,005) [3.6] | 249,417 (248,713..265,857) [3.9] | 287,691 (269,310..306,504) [3.4] | 277,906 (260,372..289,808) [3.4] | 318,260 (303,387..324,006) [3.1] | 311,872 (300,922..328,369) [3.1] | 317,255 (294,719..327,586) [3.1] | 292,843 (276,873..296,401) [3.3] | 3.46x | 3.16x | 3.65x | 3.52x | 4.03x | 3.95x | 4.02x | 3.71x |
| cpu chain-inline sampled counters | 73,978 (71,901..75,075) [13.4] | 198,317 (186,850..205,473) [5.0] | 188,703 (183,900..223,662) [5.2] | 199,001 (174,037..201,895) [5.0] | 208,524 (199,339..216,186) [4.6] | 207,714 (204,589..232,888) [4.7] | 224,323 (216,783..225,860) [4.4] | 227,373 (201,319..233,399) [4.3] | 232,169 (230,325..240,504) [4.2] | 2.68x | 2.55x | 2.69x | 2.82x | 2.81x | 3.03x | 3.07x | 3.14x |
| cpu chain-inline sampled report | 53,133 (51,949..53,812) [18.7] | 62,611 (58,018..64,927) [15.9] | 59,880 (55,395..62,559) [16.6] | 66,316 (64,969..83,785) [14.9] | 90,099 (82,173..97,389) [10.9] | 106,525 (100,932..108,749) [9.3] | 110,184 (97,083..119,303) [8.9] | 115,297 (103,636..118,748) [8.6] | 109,378 (108,579..115,688) [9.0] | 1.18x | 1.13x | 1.25x | 1.70x | 2.00x | 2.07x | 2.17x | 2.06x |
| cpu chain-inline counters none | 80,042 (78,716..84,635) [12.4] | 272,026 (257,553..282,884) [3.6] | 267,354 (199,034..277,436) [3.7] | 268,691 (267,006..310,750) [3.6] | 311,159 (310,211..339,141) [3.1] | 295,057 (290,647..314,671) [3.3] | 325,933 (324,778..328,336) [3.0] | 316,000 (309,097..345,092) [3.1] | 330,168 (325,310..346,465) [3.0] | 3.40x | 3.34x | 3.36x | 3.89x | 3.69x | 4.07x | 3.95x | 4.12x |
| cpu chain-inline counters counters | 70,902 (69,147..73,126) [14.0] | 203,959 (196,227..211,046) [4.9] | 201,857 (190,132..207,399) [4.9] | 210,239 (187,524..212,340) [4.7] | 223,247 (212,178..228,465) [4.4] | 233,910 (209,665..242,220) [4.2] | 234,014 (225,855..241,557) [4.2] | 229,882 (229,206..235,788) [4.3] | 229,179 (219,873..229,747) [4.3] | 2.88x | 2.85x | 2.97x | 3.15x | 3.30x | 3.30x | 3.24x | 3.23x |
| cpu chain-inline counters report | 65,837 (65,723..66,390) [15.1] | 104,679 (97,021..109,604) [9.5] | 102,304 (93,610..104,479) [9.7] | 123,051 (109,350..127,530) [8.0] | 138,801 (130,580..140,079) [7.0] | 166,379 (146,281..170,660) [5.9] | 158,976 (142,328..172,428) [6.2] | 163,542 (162,019..181,892) [6.0] | 164,498 (162,797..177,504) [6.0] | 1.59x | 1.55x | 1.87x | 2.11x | 2.53x | 2.41x | 2.48x | 2.50x |
| cpu chain-call all none | 29,850 (28,424..31,449) [26.9] | 55,005 (52,896..57,107) [16.9] | 52,166 (50,665..53,440) [16.9] | 54,045 (47,613..54,272) [17.3] | 54,080 (50,594..54,805) [17.1] | 51,767 (50,602..53,145) [13.5] | 51,507 (48,873..55,717) [13.3] | 54,437 (52,963..55,591) [12.9] | 55,283 (50,863..57,559) [13.1] | 1.84x | 1.75x | 1.81x | 1.81x | 1.73x | 1.73x | 1.82x | 1.85x |
| cpu chain-call all counters | 24,647 (24,118..25,535) [34.1] | 35,855 (35,339..38,492) [25.5] | 37,267 (35,255..38,397) [24.7] | 37,640 (34,086..39,370) [24.4] | 38,799 (36,466..39,678) [24.2] | 37,420 (37,058..38,201) [23.1] | 37,452 (35,501..40,044) [20.6] | 40,337 (37,676..40,626) [19.9] | 38,568 (37,631..39,593) [20.5] | 1.45x | 1.51x | 1.53x | 1.57x | 1.52x | 1.52x | 1.64x | 1.56x |
| cpu chain-call all report | 8,848 (8,797..9,339) [65.8] | 11,185 (10,587..11,990) [72.2] | 10,705 (10,651..11,009) [73.5] | 10,150 (9,525..11,345) [70.5] | 10,193 (10,082..11,456) [73.1] | 11,129 (10,947..13,009) [63.7] | 10,983 (9,875..11,204) [49.0] | 12,222 (9,797..12,400) [46.1] | 11,691 (11,350..11,888) [46.2] | 1.26x | 1.21x | 1.15x | 1.15x | 1.26x | 1.24x | 1.38x | 1.32x |
| cpu chain-call sampled none | 29,219 (28,784..29,817) [27.6] | 53,406 (50,373..55,168) [17.3] | 52,837 (51,470..54,560) [17.1] | 52,343 (49,466..55,166) [17.5] | 53,528 (52,519..54,672) [16.1] | 52,310 (48,344..55,438) [13.1] | 50,752 (48,846..54,441) [13.1] | 52,883 (51,432..55,879) [12.8] | 54,455 (51,990..55,503) [13.0] | 1.83x | 1.81x | 1.79x | 1.83x | 1.79x | 1.74x | 1.81x | 1.86x |
| cpu chain-call sampled counters | 24,763 (24,598..24,995) [33.9] | 39,088 (38,610..39,165) [25.0] | 38,459 (37,121..39,374) [24.0] | 36,744 (36,243..38,287) [24.5] | 37,236 (36,703..39,746) [24.3] | 37,353 (36,455..38,339) [22.3] | 37,926 (32,764..38,554) [20.4] | 39,700 (36,611..40,171) [19.9] | 38,561 (35,823..39,095) [20.4] | 1.58x | 1.55x | 1.48x | 1.50x | 1.51x | 1.53x | 1.60x | 1.56x |
| cpu chain-call sampled report | 17,509 (17,022..17,667) [40.0] | 22,980 (22,534..24,337) [36.9] | 23,035 (22,368..23,276) [36.6] | 21,690 (21,031..23,737) [36.1] | 21,921 (21,005..23,634) [37.9] | 22,657 (21,522..24,213) [33.3] | 22,446 (22,034..22,890) [27.7] | 23,903 (22,451..24,066) [26.2] | 23,453 (23,295..23,844) [26.9] | 1.31x | 1.32x | 1.24x | 1.25x | 1.29x | 1.28x | 1.37x | 1.34x |
| cpu chain-call counters none | 29,714 (28,555..30,259) [27.7] | 53,731 (52,513..55,139) [17.5] | 51,119 (47,548..51,859) [17.4] | 49,281 (49,159..49,359) [17.1] | 50,672 (49,530..54,409) [17.4] | 49,729 (48,630..53,596) [13.9] | 54,902 (46,845..55,149) [13.0] | 56,798 (54,283..58,871) [12.3] | 54,234 (54,080..56,037) [13.0] | 1.81x | 1.72x | 1.66x | 1.71x | 1.67x | 1.85x | 1.91x | 1.83x |
| cpu chain-call counters counters | 24,663 (23,986..25,083) [34.6] | 38,764 (37,382..40,248) [24.4] | 35,961 (33,389..38,088) [26.3] | 37,304 (37,096..38,400) [24.6] | 37,268 (35,705..38,908) [24.8] | 36,715 (35,850..39,080) [22.1] | 38,726 (34,373..38,780) [20.2] | 37,552 (37,244..40,187) [20.4] | 39,544 (38,087..40,222) [20.1] | 1.57x | 1.46x | 1.51x | 1.51x | 1.49x | 1.57x | 1.52x | 1.60x |
| cpu chain-call counters report | 23,517 (22,725..23,575) [35.4] | 33,644 (32,418..34,464) [28.4] | 33,019 (31,492..35,674) [27.5] | 32,656 (30,672..34,589) [27.4] | 33,476 (31,666..35,130) [27.1] | 33,740 (32,495..36,026) [25.4] | 33,896 (31,770..36,507) [22.4] | 34,784 (32,599..35,140) [22.3] | 34,465 (34,281..34,621) [22.5] | 1.43x | 1.40x | 1.39x | 1.42x | 1.43x | 1.44x | 1.48x | 1.47x |
| cpu each-batch all none | 40,418 (39,289..41,818) [22.7] | 113,896 (101,879..123,969) [8.7] | 107,199 (101,416..115,932) [9.1] | 113,461 (99,216..116,684) [8.7] | 104,452 (103,078..116,205) [8.5] | 105,270 (103,101..107,925) [8.7] | 107,118 (97,685..112,045) [7.9] | 104,902 (102,007..106,099) [8.0] | 107,456 (104,402..113,923) [7.9] | 2.82x | 2.65x | 2.81x | 2.58x | 2.60x | 2.65x | 2.60x | 2.66x |
| cpu each-batch all counters | 34,652 (34,017..35,169) [26.8] | 82,328 (74,723..87,498) [12.1] | 81,772 (76,467..87,487) [12.0] | 78,141 (76,399..86,905) [12.5] | 77,644 (75,198..84,155) [12.6] | 79,632 (78,258..84,908) [12.1] | 77,202 (74,997..79,684) [11.2] | 80,375 (79,059..81,735) [10.9] | 81,441 (78,504..82,562) [10.9] | 2.38x | 2.36x | 2.26x | 2.24x | 2.30x | 2.23x | 2.32x | 2.35x |
| cpu each-batch all report | 15,101 (15,043..16,045) [56.5] | 20,876 (19,801..21,620) [47.8] | 21,395 (20,706..21,509) [46.6] | 20,741 (20,111..21,708) [47.3] | 21,305 (18,817..22,436) [46.7] | 19,850 (19,753..22,766) [48.5] | 21,754 (20,617..22,508) [35.2] | 21,646 (20,046..22,465) [35.0] | 22,589 (21,730..22,777) [34.3] | 1.38x | 1.42x | 1.37x | 1.41x | 1.31x | 1.44x | 1.43x | 1.50x |
| cpu each-batch sampled none | 40,521 (40,112..42,860) [22.4] | 107,218 (106,653..116,271) [9.2] | 105,879 (96,576..106,038) [9.3] | 108,236 (107,247..118,532) [9.1] | 104,942 (92,616..117,598) [8.4] | 104,014 (91,988..114,381) [8.0] | 97,196 (91,569..100,746) [8.4] | 107,588 (107,098..109,512) [7.5] | 109,580 (105,114..111,779) [7.8] | 2.65x | 2.61x | 2.67x | 2.59x | 2.57x | 2.40x | 2.66x | 2.70x |
| cpu each-batch sampled counters | 36,227 (33,949..37,037) [25.7] | 82,785 (82,653..86,139) [12.0] | 81,680 (73,981..82,269) [12.1] | 81,426 (80,871..85,944) [12.0] | 83,610 (76,829..87,045) [11.5] | 86,306 (84,367..89,080) [11.2] | 78,524 (72,259..80,436) [11.1] | 81,416 (75,703..82,109) [10.8] | 81,509 (76,883..83,319) [10.9] | 2.29x | 2.25x | 2.25x | 2.31x | 2.38x | 2.17x | 2.25x | 2.25x |
| cpu each-batch sampled report | 25,553 (24,614..26,376) [33.3] | 43,975 (43,169..45,694) [22.6] | 41,829 (41,302..42,063) [23.8] | 44,235 (40,673..45,216) [22.3] | 45,038 (39,922..48,754) [21.6] | 47,497 (47,186..52,148) [19.0] | 43,734 (42,306..49,080) [17.1] | 46,853 (44,807..49,823) [15.8] | 45,024 (44,635..47,185) [16.1] | 1.72x | 1.64x | 1.73x | 1.76x | 1.86x | 1.71x | 1.83x | 1.76x |
| cpu each-batch counters none | 40,044 (38,393..41,056) [23.1] | 112,914 (111,132..123,109) [8.7] | 102,833 (95,777..112,708) [9.5] | 120,755 (93,618..121,126) [8.2] | 117,677 (109,145..128,673) [8.4] | 106,851 (99,980..109,257) [8.1] | 106,725 (99,257..108,270) [7.9] | 103,131 (100,723..104,342) [8.0] | 105,619 (105,531..111,827) [7.7] | 2.82x | 2.57x | 3.02x | 2.94x | 2.67x | 2.67x | 2.58x | 2.64x |
| cpu each-batch counters counters | 35,997 (35,405..36,067) [26.1] | 86,573 (81,852..87,858) [11.4] | 83,710 (71,874..85,214) [11.9] | 82,460 (78,340..83,237) [12.0] | 84,313 (73,111..87,278) [11.6] | 80,456 (79,753..83,125) [11.7] | 74,924 (73,603..79,696) [11.6] | 78,579 (77,835..80,937) [11.1] | 82,308 (77,367..83,077) [10.7] | 2.41x | 2.33x | 2.29x | 2.34x | 2.24x | 2.08x | 2.18x | 2.29x |
| cpu each-batch counters report | 32,958 (32,321..33,524) [28.3] | 71,736 (68,745..71,890) [13.8] | 71,274 (71,023..71,826) [13.9] | 69,602 (69,373..75,157) [14.2] | 69,072 (66,815..70,013) [14.2] | 71,324 (63,111..72,599) [13.6] | 64,551 (63,177..66,112) [13.3] | 70,636 (63,147..71,265) [12.4] | 69,610 (63,258..72,016) [12.6] | 2.18x | 2.16x | 2.11x | 2.10x | 2.16x | 1.96x | 2.14x | 2.11x |
| mixed all none | 3,772 (3,481..4,133) [113.2] | 4,942 (4,725..5,343) [112.4] | 4,990 (4,742..5,252) [107.3] | 4,553 (4,447..5,093) [95.9] | 4,448 (4,434..4,485) [81.0] | 4,670 (4,310..5,151) [71.6] | 4,657 (4,050..5,215) [70.7] | 5,066 (4,645..5,300) [67.5] | 5,186 (5,068..5,241) [67.2] | 1.31x | 1.32x | 1.21x | 1.18x | 1.24x | 1.23x | 1.34x | 1.37x |
| mixed all counters | 3,778 (3,416..4,341) [108.5] | 4,790 (4,747..5,334) [116.9] | 4,827 (4,669..4,920) [108.8] | 4,317 (4,256..4,885) [102.9] | 4,421 (4,298..4,954) [88.7] | 4,378 (4,020..5,332) [82.5] | 4,264 (4,116..5,438) [81.7] | 4,871 (4,357..5,270) [71.9] | 5,084 (4,581..5,094) [73.1] | 1.27x | 1.28x | 1.14x | 1.17x | 1.16x | 1.13x | 1.29x | 1.35x |
| mixed all report | 2,492 (2,432..2,609) [159.5] | 2,949 (2,935..3,230) [189.3] | 3,067 (3,032..3,254) [165.8] | 2,751 (2,725..3,027) [163.6] | 2,852 (2,811..3,072) [129.8] | 2,747 (2,646..3,401) [126.1] | 2,913 (2,702..3,271) [111.9] | 3,283 (2,890..3,439) [101.4] | 3,220 (3,212..3,236) [107.8] | 1.18x | 1.23x | 1.10x | 1.14x | 1.10x | 1.17x | 1.32x | 1.29x |
| mixed sampled none | 3,668 (3,559..4,455) [106.4] | 5,011 (4,686..5,197) [117.9] | 5,071 (4,860..5,397) [105.8] | 4,486 (4,425..5,116) [97.9] | 5,041 (4,428..5,209) [70.7] | 4,785 (4,185..5,812) [65.7] | 4,568 (4,154..5,125) [70.7] | 5,486 (4,632..5,512) [58.7] | 5,518 (5,392..5,604) [62.1] | 1.37x | 1.38x | 1.22x | 1.37x | 1.30x | 1.25x | 1.50x | 1.50x |
| mixed sampled counters | 3,509 (3,484..4,266) [119.9] | 4,554 (4,493..5,283) [121.4] | 4,920 (4,725..5,111) [108.5] | 4,394 (4,253..5,068) [103.7] | 4,628 (4,214..4,858) [83.5] | 4,275 (4,171..5,271) [79.9] | 4,138 (4,081..5,337) [82.9] | 5,129 (4,376..5,285) [70.8] | 5,104 (4,834..5,111) [74.2] | 1.30x | 1.40x | 1.25x | 1.32x | 1.22x | 1.18x | 1.46x | 1.45x |
| mixed sampled report | 3,444 (3,242..4,050) [122.8] | 4,285 (3,940..4,608) [134.0] | 4,054 (3,709..4,401) [119.7] | 4,012 (3,990..4,529) [113.2] | 4,056 (3,802..4,355) [94.8] | 3,962 (3,679..4,833) [92.2] | 3,958 (3,767..4,798) [89.3] | 4,768 (4,202..4,814) [76.1] | 4,605 (4,342..4,714) [82.7] | 1.24x | 1.18x | 1.16x | 1.18x | 1.15x | 1.15x | 1.38x | 1.34x |
| mixed counters none | 3,790 (3,571..4,589) [109.2] | 4,927 (2,083..5,451) [117.6] | 5,007 (4,592..5,048) [107.0] | 4,731 (4,438..4,941) [92.2] | 4,846 (4,780..5,145) [65.6] | 4,619 (4,338..5,707) [66.4] | 4,684 (4,122..5,537) [65.9] | 5,352 (4,659..5,539) [65.1] | 5,143 (5,058..5,351) [64.1] | 1.30x | 1.32x | 1.25x | 1.28x | 1.22x | 1.24x | 1.41x | 1.36x |
| mixed counters counters | 3,719 (3,642..4,062) [113.4] | 4,557 (4,520..5,172) [125.5] | 4,877 (4,765..5,186) [105.4] | 4,283 (4,234..4,556) [105.1] | 4,543 (4,472..5,024) [82.4] | 4,576 (4,321..5,239) [78.8] | 4,385 (4,110..5,380) [77.9] | 5,069 (4,302..5,364) [69.7] | 5,048 (4,648..5,074) [74.1] | 1.23x | 1.31x | 1.15x | 1.22x | 1.23x | 1.18x | 1.36x | 1.36x |
| mixed counters report | 3,630 (3,493..4,287) [114.5] | 4,620 (4,146..4,763) [121.6] | 4,774 (4,696..5,000) [108.8] | 4,346 (4,192..4,538) [100.7] | 4,463 (4,365..4,597) [88.6] | 4,359 (4,065..5,210) [81.7] | 4,312 (3,774..5,102) [76.9] | 4,658 (4,472..5,074) [79.0] | 5,062 (4,727..5,079) [76.0] | 1.27x | 1.32x | 1.20x | 1.23x | 1.20x | 1.19x | 1.28x | 1.39x |
| mixed example-default | 1,291 (1,276..1,399) [515.6] | 1,651 (1,629..1,753) [405.2] | 1,708 (1,702..1,810) [376.9] | 1,611 (1,609..1,784) [377.6] | 1,624 (1,591..1,745) [372.3] | 1,635 (1,584..1,818) [363.9] | 1,623 (1,540..1,839) [365.9] | 1,759 (1,633..1,860) [342.6] | 1,759 (1,753..1,760) [351.4] | 1.28x | 1.32x | 1.25x | 1.26x | 1.27x | 1.26x | 1.36x | 1.36x |

## P6 study B: knob defaults

Calls/s, median (min..max) over rounds (one round per child, every variant per round); dispatcher us/call median in brackets.

| config | python | rust-o0 | rust-o20 | rust-o100 | rust-o300 | rust-o1000 | rust-o5000 | rust-oinf | rust-onone | rust-o0/python | rust-o20/python | rust-o100/python | rust-o300/python | rust-o1000/python | rust-o5000/python | rust-oinf/python | rust-onone/python |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| chain-inline defaults | 72,396 (72,105..77,610) [13.7] | 191,580 (185,462..192,486) [5.1] | 182,787 (172,518..203,532) [5.4] | 224,137 (216,414..243,592) [4.4] | 230,050 (227,571..242,005) [4.3] | 233,821 (225,819..239,356) [4.2] | 232,935 (226,041..239,336) [4.2] | 241,621 (230,361..248,031) [4.1] | 230,738 (205,680..248,359) [4.2] | 2.65x | 2.52x | 3.10x | 3.18x | 3.23x | 3.22x | 3.34x | 3.19x |
| chain-inline window 16 | 75,799 (75,307..76,929) [13.1] | 194,850 (187,750..202,748) [5.1] | 201,607 (187,646..214,718) [4.9] | 227,880 (215,001..245,417) [4.3] | 233,963 (221,619..256,365) [4.1] | 259,182 (248,066..264,874) [3.8] | 247,429 (242,500..259,133) [3.9] | 245,146 (226,328..261,271) [4.0] | 245,867 (221,889..264,454) [4.0] | 2.57x | 2.66x | 3.01x | 3.09x | 3.42x | 3.26x | 3.23x | 3.24x |
| chain-inline window 256 | 74,667 (71,400..77,140) [13.3] | 184,230 (183,654..186,249) [5.3] | 186,162 (183,520..208,195) [5.3] | 234,701 (217,311..238,217) [4.2] | 227,260 (221,328..240,540) [4.3] | 250,464 (244,016..262,237) [3.9] | 251,228 (248,360..255,106) [3.9] | 252,438 (248,714..271,972) [3.9] | 234,859 (226,037..242,660) [4.2] | 2.47x | 2.49x | 3.14x | 3.04x | 3.35x | 3.36x | 3.38x | 3.15x |
| chain-inline retire_buffer 1024 | 74,787 (74,375..75,769) [13.3] | 189,635 (176,661..198,240) [5.2] | 205,825 (184,823..212,035) [4.8] | 234,054 (224,899..254,344) [4.2] | 227,891 (214,310..251,884) [4.2] | 245,616 (244,895..249,630) [4.0] | 259,452 (246,668..264,017) [3.8] | 251,786 (245,624..265,967) [3.9] | 244,426 (241,880..251,632) [4.0] | 2.54x | 2.75x | 3.13x | 3.05x | 3.28x | 3.47x | 3.37x | 3.27x |
| chain-inline event_sample 0.0 | 73,612 (72,185..81,319) [13.5] | 202,231 (171,829..219,239) [4.9] | 205,260 (201,092..222,610) [4.7] | 237,763 (219,862..241,917) [4.1] | 237,510 (232,805..260,033) [4.1] | 248,361 (245,278..255,645) [3.9] | 260,757 (249,225..261,768) [3.8] | 253,254 (237,898..256,284) [3.9] | 241,827 (237,017..252,281) [4.1] | 2.75x | 2.79x | 3.23x | 3.23x | 3.37x | 3.54x | 3.44x | 3.29x |
| chain-inline event_sample 1.0 | 76,484 (74,724..76,661) [13.0] | 189,195 (157,552..191,165) [5.2] | 205,309 (184,237..220,465) [4.8] | 234,055 (216,632..243,554) [4.2] | 237,480 (226,660..240,712) [4.1] | 253,813 (253,696..268,518) [3.8] | 248,520 (231,045..251,480) [3.9] | 242,200 (240,288..245,619) [4.0] | 251,501 (249,505..255,050) [3.9] | 2.47x | 2.68x | 3.06x | 3.10x | 3.32x | 3.25x | 3.17x | 3.29x |
| chain-inline Fixed(16) | 71,723 (69,849..73,009) [13.8] | 137,008 (129,272..157,508) [7.2] | 205,533 (195,398..242,985) [4.8] | 266,543 (243,863..270,236) [3.7] | 271,815 (258,443..273,889) [3.5] | 269,184 (257,607..278,791) [3.6] | 270,090 (268,719..284,013) [3.6] | 268,787 (260,484..270,336) [3.6] | 264,980 (246,637..271,703) [3.7] | 1.91x | 2.87x | 3.72x | 3.79x | 3.75x | 3.77x | 3.75x | 3.69x |
| chain-inline Fixed(256) | 75,504 (71,813..75,717) [13.2] | 193,363 (172,237..197,586) [5.1] | 197,407 (194,263..215,191) [5.0] | 212,843 (193,903..233,203) [4.6] | 220,451 (218,881..233,789) [4.5] | 226,641 (215,855..229,830) [4.3] | 226,564 (196,052..226,572) [4.3] | 224,174 (211,321..243,925) [4.4] | 217,264 (214,753..227,707) [4.5] | 2.56x | 2.61x | 2.82x | 2.92x | 3.00x | 3.00x | 2.97x | 2.88x |
| chain-call defaults | 26,252 (26,202..26,345) [32.1] | 37,141 (36,999..37,789) [25.2] | 36,999 (35,320..37,163) [24.7] | 39,803 (37,070..40,988) [23.2] | 37,890 (36,540..43,267) [24.3] | 39,449 (36,619..40,383) [21.9] | 39,210 (36,676..39,451) [20.1] | 37,459 (36,972..38,635) [20.3] | 38,198 (36,151..38,562) [20.6] | 1.41x | 1.41x | 1.52x | 1.44x | 1.50x | 1.49x | 1.43x | 1.46x |
| chain-call window 16 | 26,158 (26,089..26,859) [32.5] | 36,875 (35,370..38,020) [24.9] | 37,097 (35,744..37,592) [24.7] | 40,312 (35,803..41,492) [23.7] | 38,956 (38,907..39,233) [23.8] | 39,330 (37,438..40,225) [21.8] | 38,926 (38,756..39,018) [20.3] | 37,731 (37,331..39,012) [20.2] | 37,961 (37,262..38,285) [20.4] | 1.41x | 1.42x | 1.54x | 1.49x | 1.50x | 1.49x | 1.44x | 1.45x |
| chain-call window 256 | 26,650 (26,115..26,971) [31.8] | 37,922 (37,677..38,658) [24.6] | 37,247 (36,306..40,774) [24.5] | 39,152 (32,579..41,157) [23.2] | 38,535 (38,223..40,707) [23.9] | 38,434 (32,686..40,212) [22.6] | 39,571 (37,489..39,887) [19.8] | 39,521 (37,789..39,567) [19.6] | 38,117 (36,005..39,722) [20.5] | 1.42x | 1.40x | 1.47x | 1.45x | 1.44x | 1.48x | 1.48x | 1.43x |
| chain-call retire_buffer 1024 | 25,643 (25,483..27,264) [33.0] | 37,711 (36,496..38,745) [24.8] | 37,843 (33,697..40,353) [24.3] | 39,726 (36,490..39,939) [24.3] | 38,990 (36,190..40,688) [23.3] | 38,730 (36,188..39,932) [21.7] | 39,252 (37,266..40,684) [20.2] | 38,682 (36,292..40,656) [19.7] | 39,197 (37,347..41,380) [20.0] | 1.47x | 1.48x | 1.55x | 1.52x | 1.51x | 1.53x | 1.51x | 1.53x |
| chain-call event_sample 0.0 | 26,867 (25,552..27,103) [31.9] | 36,836 (35,986..38,175) [24.8] | 37,210 (35,850..39,769) [24.7] | 38,328 (36,917..40,293) [24.7] | 38,345 (36,468..41,782) [24.2] | 36,204 (35,545..39,062) [22.6] | 39,018 (37,021..39,765) [20.1] | 38,596 (36,793..39,558) [20.0] | 39,515 (35,178..41,467) [20.0] | 1.37x | 1.38x | 1.43x | 1.43x | 1.35x | 1.45x | 1.44x | 1.47x |
| chain-call event_sample 1.0 | 26,354 (25,787..27,100) [32.2] | 36,805 (35,630..37,184) [24.2] | 35,685 (35,657..37,504) [25.2] | 37,119 (36,666..38,884) [24.3] | 38,623 (36,145..42,166) [23.0] | 36,643 (36,505..38,440) [22.6] | 39,440 (36,432..40,967) [20.1] | 39,539 (37,357..40,695) [19.6] | 37,345 (36,236..38,681) [20.6] | 1.40x | 1.35x | 1.41x | 1.47x | 1.39x | 1.50x | 1.50x | 1.42x |
| chain-call Fixed(16) | 19,748 (19,433..21,033) [38.0] | 25,653 (23,890..27,013) [34.3] | 24,326 (22,921..28,602) [33.9] | 27,005 (24,374..27,108) [32.9] | 28,912 (28,121..30,580) [26.8] | 27,888 (24,430..33,593) [24.8] | 29,385 (28,193..32,149) [23.7] | 29,041 (23,021..32,500) [23.7] | 27,284 (27,209..30,099) [25.0] | 1.30x | 1.23x | 1.37x | 1.46x | 1.41x | 1.49x | 1.47x | 1.38x |
| chain-call Fixed(256) | 25,706 (25,491..26,424) [32.9] | 36,132 (35,955..36,811) [25.8] | 35,243 (34,025..37,353) [25.1] | 35,749 (35,645..40,875) [25.0] | 36,728 (35,977..40,927) [24.8] | 38,734 (36,615..40,081) [22.6] | 37,239 (35,479..37,344) [21.3] | 37,369 (34,456..39,163) [20.4] | 38,038 (36,064..38,823) [20.7] | 1.41x | 1.37x | 1.39x | 1.43x | 1.51x | 1.45x | 1.45x | 1.48x |
| chain-call TargetLoad | 26,283 (25,298..26,361) [32.5] | 36,169 (35,845..37,142) [25.2] | 35,333 (33,309..37,329) [25.7] | 36,515 (35,374..36,739) [25.1] | 38,518 (35,243..39,083) [24.0] | 39,380 (35,152..40,618) [21.7] | 38,294 (34,651..40,256) [20.1] | 37,667 (37,375..39,940) [20.3] | 37,886 (34,393..39,257) [20.7] | 1.38x | 1.34x | 1.39x | 1.47x | 1.50x | 1.46x | 1.43x | 1.44x |
| each-batch defaults | 35,571 (34,682..36,305) [26.4] | 81,440 (80,722..83,257) [12.1] | 84,601 (80,893..89,663) [11.7] | 87,230 (79,420..87,521) [11.3] | 86,183 (78,710..96,081) [11.5] | 92,556 (78,733..95,557) [10.6] | 75,215 (70,138..77,244) [11.7] | 79,276 (74,100..80,957) [11.2] | 77,774 (70,938..78,165) [11.3] | 2.29x | 2.38x | 2.45x | 2.42x | 2.60x | 2.11x | 2.23x | 2.19x |
| each-batch window 16 | 35,199 (33,819..36,218) [27.0] | 73,007 (69,106..78,647) [13.0] | 77,056 (72,951..78,806) [12.7] | 80,657 (74,007..85,688) [12.2] | 84,443 (76,408..91,555) [11.6] | 81,284 (78,978..85,382) [11.4] | 79,715 (75,292..81,338) [10.9] | 78,698 (78,376..79,418) [10.9] | 75,475 (67,934..79,960) [11.6] | 2.07x | 2.19x | 2.29x | 2.40x | 2.31x | 2.26x | 2.24x | 2.14x |
| each-batch window 256 | 37,372 (35,801..37,581) [25.0] | 87,745 (84,183..88,318) [11.3] | 88,536 (79,074..89,822) [11.1] | 87,715 (78,079..88,628) [11.2] | 81,470 (75,739..87,384) [11.6] | 77,908 (77,075..85,387) [11.4] | 80,795 (74,175..82,084) [10.8] | 71,736 (68,261..79,056) [12.0] | 75,892 (69,633..83,109) [11.5] | 2.35x | 2.37x | 2.35x | 2.18x | 2.08x | 2.16x | 1.92x | 2.03x |
| each-batch retire_buffer 1024 | 35,556 (32,836..36,191) [26.5] | 80,766 (77,870..81,078) [12.2] | 84,677 (76,963..89,824) [11.3] | 83,020 (82,283..89,543) [11.8] | 87,461 (85,276..94,803) [11.2] | 83,870 (82,942..89,373) [10.9] | 79,497 (70,419..81,151) [10.9] | 74,173 (71,468..78,183) [11.6] | 78,534 (74,642..78,846) [11.3] | 2.27x | 2.38x | 2.33x | 2.46x | 2.36x | 2.24x | 2.09x | 2.21x |
| each-batch event_sample 0.0 | 35,590 (35,559..36,363) [26.4] | 83,478 (74,252..84,674) [11.9] | 87,358 (84,189..89,700) [11.3] | 84,444 (82,898..92,087) [11.7] | 87,801 (79,183..93,899) [11.2] | 87,214 (79,156..90,082) [11.2] | 79,050 (77,710..83,278) [11.2] | 77,253 (67,453..83,968) [11.4] | 80,439 (73,092..82,212) [10.9] | 2.35x | 2.45x | 2.37x | 2.47x | 2.45x | 2.22x | 2.17x | 2.26x |
| each-batch event_sample 1.0 | 35,244 (34,029..35,672) [26.8] | 81,206 (70,557..89,635) [12.1] | 80,716 (78,579..92,574) [12.2] | 83,460 (82,749..86,884) [11.8] | 88,695 (85,539..92,941) [11.1] | 86,106 (81,909..90,579) [11.4] | 77,833 (74,287..78,831) [11.3] | 73,109 (67,523..77,497) [11.6] | 79,045 (78,434..79,964) [11.1] | 2.30x | 2.29x | 2.37x | 2.52x | 2.44x | 2.21x | 2.07x | 2.24x |
| each-batch Fixed(16) | 36,066 (35,889..37,399) [26.0] | 82,190 (76,533..82,194) [12.0] | 81,415 (74,471..82,778) [12.1] | 84,499 (84,273..86,039) [11.4] | 81,318 (78,666..83,604) [12.0] | 86,690 (80,843..89,128) [11.2] | 80,962 (77,095..82,019) [10.7] | 77,399 (74,625..81,841) [11.2] | 79,742 (72,703..80,034) [11.0] | 2.28x | 2.26x | 2.34x | 2.25x | 2.40x | 2.24x | 2.15x | 2.21x |
| each-batch Fixed(256) | 37,699 (36,221..38,888) [24.8] | 78,447 (62,696..79,467) [12.6] | 80,913 (77,385..84,649) [12.2] | 81,136 (80,839..83,080) [12.1] | 85,921 (80,384..86,169) [11.5] | 84,695 (80,032..85,170) [11.6] | 77,128 (75,812..81,768) [11.2] | 83,087 (77,833..83,219) [10.8] | 79,223 (75,659..80,532) [11.1] | 2.08x | 2.15x | 2.15x | 2.28x | 2.25x | 2.05x | 2.20x | 2.10x |
| each-batch TargetLoad | 36,295 (35,281..37,917) [25.7] | 81,465 (79,088..82,884) [12.1] | 84,215 (76,056..84,695) [11.7] | 81,674 (81,235..83,540) [12.1] | 81,742 (80,534..81,896) [12.1] | 84,708 (81,151..91,229) [11.6] | 77,751 (77,154..79,243) [11.3] | 72,993 (54,853..82,574) [11.7] | 78,150 (71,684..81,409) [11.4] | 2.24x | 2.32x | 2.25x | 2.25x | 2.33x | 2.14x | 2.01x | 2.15x |
| trace_replay defaults | 603 (600..604) [80.2] | 602 (597..605) [60.3] | 604 (604..604) [59.6] | 602 (601..605) [52.2] | 600 (600..601) [56.1] | 604 (601..605) [50.3] | 603 (602..605) [53.1] | 602 (601..605) [52.4] | 603 (603..603) [49.9] | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x |
| trace_replay window 16 | 602 (602..605) [86.0] | 601 (600..604) [66.4] | 602 (602..605) [63.0] | 603 (603..604) [49.4] | 602 (601..604) [62.3] | 602 (600..605) [49.8] | 605 (602..605) [49.8] | 603 (602..604) [48.3] | 606 (604..606) [52.0] | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.01x |
| trace_replay window 256 | 602 (597..602) [83.9] | 603 (600..606) [69.6] | 602 (601..604) [60.7] | 602 (602..603) [49.9] | 601 (599..604) [56.0] | 601 (598..604) [50.3] | 600 (599..603) [52.1] | 605 (604..606) [51.7] | 604 (602..606) [51.6] | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.01x | 1.00x |
| trace_replay retire_buffer 1024 | 600 (599..604) [85.1] | 602 (601..603) [67.1] | 604 (602..606) [61.6] | 603 (600..605) [51.5] | 601 (600..601) [54.9] | 605 (603..605) [50.5] | 605 (600..605) [51.3] | 603 (600..604) [49.9] | 603 (600..606) [50.5] | 1.00x | 1.01x | 1.00x | 1.00x | 1.01x | 1.01x | 1.00x | 1.00x |
| trace_replay event_sample 0.0 | 600 (600..603) [88.1] | 600 (598..603) [69.8] | 603 (602..604) [60.8] | 604 (603..606) [55.4] | 601 (598..605) [55.8] | 602 (601..605) [54.1] | 601 (600..608) [51.2] | 604 (601..604) [55.4] | 602 (601..605) [49.3] | 1.00x | 1.00x | 1.01x | 1.00x | 1.00x | 1.00x | 1.01x | 1.00x |
| trace_replay event_sample 1.0 | 602 (600..602) [79.7] | 604 (601..606) [63.4] | 605 (604..606) [61.3] | 600 (600..602) [52.1] | 604 (602..605) [55.5] | 603 (601..606) [51.7] | 604 (604..605) [50.8] | 601 (600..602) [51.7] | 601 (601..602) [48.6] | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x |
| trace_replay Fixed(16) | 318 (317..323) [101.0] | 322 (322..323) [82.8] | 319 (316..320) [80.9] | 321 (320..322) [71.4] | 321 (320..323) [72.7] | 320 (318..321) [67.2] | 319 (319..319) [70.2] | 320 (317..322) [65.5] | 319 (318..323) [63.7] | 1.01x | 1.00x | 1.01x | 1.01x | 1.01x | 1.01x | 1.01x | 1.00x |
| trace_replay Fixed(256) | 601 (600..601) [78.1] | 601 (600..605) [72.3] | 602 (602..604) [65.4] | 604 (601..604) [48.3] | 601 (601..607) [55.5] | 602 (601..602) [51.4] | 601 (601..603) [60.0] | 603 (600..605) [52.1] | 605 (602..606) [52.9] | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.01x |
| trace_replay TargetLoad | 603 (602..604) [79.7] | 604 (601..606) [72.2] | 605 (604..606) [65.3] | 602 (602..606) [51.5] | 604 (603..604) [59.6] | 602 (601..605) [50.6] | 601 (600..604) [48.0] | 602 (601..605) [50.7] | 603 (603..605) [53.3] | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x | 1.00x |

## GIL contention

Engine items/s fed directly, median (min..max) over rounds; share of its idle median; dispatcher calls/s and GIL offers/s (Rust core) medians in the same window.

### torch / thread

| variant | idle | 100/s | 300/s | 1000/s | ceiling |
|---|---|---|---|---|---|
| python | 8,927 (8,109..9,003) 100%, 0 calls/s | 6,730 (5,937..7,368) 75%, 2,007 calls/s | 5,399 (4,733..5,425) 60%, 6,021 calls/s | 767 (563..998) 9%, 20,016 calls/s | 307 (307..332) 3%, 25,745 calls/s |
| rust-o0 | 8,390 (8,084..9,054) 100%, 0 calls/s, 0 offers/s | 7,343 (7,319..8,009) 88%, 1,999 calls/s, 2,723 offers/s | 6,909 (6,550..6,960) 82%, 5,998 calls/s, 2,925 offers/s | 4,195 (3,812..4,478) 50%, 19,995 calls/s, 3,620 offers/s | 562 (511..639) 7%, 44,490 calls/s, 3,583 offers/s |
| rust-o20 | 8,876 (8,442..9,158) 100%, 0 calls/s, 0 offers/s | 7,317 (7,089..8,112) 82%, 1,999 calls/s, 1,469 offers/s | 6,372 (6,370..7,446) 72%, 5,988 calls/s, 2,471 offers/s | 3,710 (3,275..4,555) 42%, 20,038 calls/s, 3,148 offers/s | 512 (511..563) 6%, 45,507 calls/s, 2,590 offers/s |
| rust-o100 | 9,055 (8,211..9,335) 100%, 0 calls/s, 0 offers/s | 7,703 (7,011..8,138) 85%, 2,007 calls/s, 112 offers/s | 6,577 (6,115..7,140) 73%, 5,981 calls/s, 411 offers/s | 3,838 (3,351..4,375) 42%, 19,995 calls/s, 1,634 offers/s | 537 (511..562) 6%, 44,984 calls/s, 1,736 offers/s |
| rust-o300 | 8,441 (8,288..8,901) 100%, 0 calls/s, 0 offers/s | 7,089 (6,603..8,035) 84%, 1,999 calls/s, 1 offers/s | 6,269 (6,140..7,166) 74%, 6,006 calls/s, 11 offers/s | 3,274 (3,019..4,554) 39%, 19,950 calls/s, 383 offers/s | 486 (485..512) 6%, 46,010 calls/s, 968 offers/s |
| rust-o1000 | 8,338 (8,314..8,493) 100%, 0 calls/s, 0 offers/s | 7,318 (7,267..7,574) 88%, 2,007 calls/s, 0 offers/s | 6,500 (6,244..6,808) 78%, 5,998 calls/s, 0 offers/s | 3,837 (3,787..4,196) 46%, 19,982 calls/s, 0 offers/s | 384 (384..409) 5%, 47,574 calls/s, 89 offers/s |
| rust-o5000 | 8,211 (8,134..8,900) 100%, 0 calls/s, 0 offers/s | 7,267 (7,242..7,804) 89%, 2,007 calls/s, 0 offers/s | 6,679 (6,166..6,909) 81%, 5,989 calls/s, 0 offers/s | 3,940 (3,684..4,400) 48%, 20,012 calls/s, 0 offers/s | 409 (383..409) 5%, 46,533 calls/s, 0 offers/s |
| rust-oinf | 8,440 (8,315..8,749) 100%, 0 calls/s, 0 offers/s | 7,242 (7,115..7,702) 86%, 1,991 calls/s, 0 offers/s | 6,448 (6,398..6,500) 76%, 5,982 calls/s, 0 offers/s | 3,709 (3,454..4,161) 44%, 20,022 calls/s, 0 offers/s | 409 (409..409) 5%, 47,547 calls/s, 0 offers/s |
| rust-onone | 8,236 (8,084..8,747) 100%, 0 calls/s, 0 offers/s | 7,369 (6,449..7,703) 89%, 1,999 calls/s, 0 offers/s | 6,525 (6,065..6,602) 79%, 5,997 calls/s, 0 offers/s | 3,735 (3,531..3,736) 45%, 20,044 calls/s, 0 offers/s | 409 (384..409) 5%, 47,061 calls/s, 0 offers/s |

### torch / process

| variant | idle | 100/s | 300/s | 1000/s | ceiling |
|---|---|---|---|---|---|
| python | 7,395 (6,474..7,709) 100%, 0 calls/s | 7,474 (6,347..7,474) 101%, 1,992 calls/s | 7,449 (6,552..7,573) 101%, 6,007 calls/s | 895 (793..921) 12%, 20,040 calls/s | 511 (511..537) 7%, 24,520 calls/s |
| rust-o0 | 7,623 (5,962..7,625) 100%, 0 calls/s, 0 offers/s | 7,448 (6,194..7,754) 98%, 1,999 calls/s, 2,723 offers/s | 7,397 (6,143..7,525) 97%, 5,991 calls/s, 2,924 offers/s | 7,037 (6,219..7,243) 92%, 19,952 calls/s, 3,609 offers/s | 1,739 (1,661..2,019) 23%, 39,896 calls/s, 3,206 offers/s |
| rust-o20 | 7,369 (5,936..7,805) 100%, 0 calls/s, 0 offers/s | 7,499 (6,041..7,679) 102%, 2,008 calls/s, 1,448 offers/s | 7,345 (6,450..7,780) 100%, 6,015 calls/s, 2,523 offers/s | 6,961 (6,116..7,651) 94%, 20,026 calls/s, 3,132 offers/s | 1,916 (1,867..2,021) 26%, 39,893 calls/s, 2,296 offers/s |
| rust-o100 | 7,395 (7,343..7,421) 100%, 0 calls/s, 0 offers/s | 7,346 (7,192..7,474) 99%, 2,000 calls/s, 88 offers/s | 7,242 (7,141..7,320) 98%, 6,030 calls/s, 270 offers/s | 7,037 (6,773..7,267) 95%, 20,024 calls/s, 1,555 offers/s | 2,020 (1,791..2,044) 27%, 40,921 calls/s, 1,615 offers/s |
| rust-o300 | 7,676 (6,115..7,906) 100%, 0 calls/s, 0 offers/s | 7,576 (5,784..7,857) 99%, 2,000 calls/s, 0 offers/s | 7,268 (6,015..7,780) 95%, 5,982 calls/s, 16 offers/s | 6,987 (6,346..7,499) 91%, 20,034 calls/s, 155 offers/s | 1,970 (1,893..2,020) 26%, 40,417 calls/s, 853 offers/s |
| rust-o1000 | 7,062 (6,115..7,497) 100%, 0 calls/s, 0 offers/s | 7,295 (5,887..7,397) 103%, 2,000 calls/s, 0 offers/s | 7,371 (5,964..7,372) 104%, 6,007 calls/s, 0 offers/s | 6,808 (6,139..7,192) 96%, 20,004 calls/s, 1 offers/s | 537 (537..588) 8%, 43,958 calls/s, 108 offers/s |
| rust-o5000 | 7,241 (5,936..7,548) 100%, 0 calls/s, 0 offers/s | 7,422 (6,142..7,601) 103%, 1,999 calls/s, 0 offers/s | 7,294 (6,244..7,602) 101%, 6,007 calls/s, 0 offers/s | 6,910 (6,039..7,345) 95%, 20,045 calls/s, 1 offers/s | 562 (537..563) 8%, 43,989 calls/s, 0 offers/s |
| rust-oinf | 7,446 (7,396..7,522) 100%, 0 calls/s, 0 offers/s | 7,346 (7,294..7,474) 99%, 2,000 calls/s, 0 offers/s | 7,346 (7,192..7,627) 99%, 5,990 calls/s, 0 offers/s | 6,936 (6,859..7,116) 93%, 19,981 calls/s, 0 offers/s | 537 (537..537) 7%, 44,479 calls/s, 0 offers/s |
| rust-onone | 7,344 (5,168..7,599) 100%, 0 calls/s, 0 offers/s | 7,372 (4,760..7,576) 100%, 2,000 calls/s, 0 offers/s | 7,398 (5,784..7,704) 101%, 5,999 calls/s, 0 offers/s | 6,884 (5,324..7,243) 94%, 20,013 calls/s, 0 offers/s | 562 (512..563) 8%, 44,490 calls/s, 0 offers/s |

### python / thread

| variant | idle | 100/s | 300/s | 1000/s | ceiling |
|---|---|---|---|---|---|
| python | 427 (404..429) 100%, 0 calls/s | 381 (380..392) 89%, 1,992 calls/s | 384 (354..390) 90%, 1,279 calls/s | 390 (369..394) 91%, 1,467 calls/s | 389 (356..390) 91%, 2,014 calls/s |
| rust-o0 | 418 (368..426) 100%, 0 calls/s, 0 offers/s | 383 (368..405) 92%, 1,989 calls/s, 132 offers/s | 390 (360..397) 93%, 1,185 calls/s, 103 offers/s | 389 (375..409) 93%, 1,019 calls/s, 99 offers/s | 393 (367..398) 94%, 1,479 calls/s, 83 offers/s |
| rust-o20 | 398 (382..413) 100%, 0 calls/s, 0 offers/s | 390 (363..398) 98%, 2,014 calls/s, 89 offers/s | 401 (390..401) 101%, 1,497 calls/s, 84 offers/s | 400 (396..405) 101%, 1,505 calls/s, 76 offers/s | 408 (403..410) 102%, 1,488 calls/s, 64 offers/s |
| rust-o100 | 414 (405..416) 100%, 0 calls/s, 0 offers/s | 391 (381..407) 94%, 1,953 calls/s, 47 offers/s | 406 (392..407) 98%, 1,509 calls/s, 54 offers/s | 408 (394..411) 99%, 1,493 calls/s, 45 offers/s | 397 (395..412) 96%, 997 calls/s, 42 offers/s |
| rust-o300 | 413 (380..413) 100%, 0 calls/s, 0 offers/s | 380 (370..402) 92%, 1,965 calls/s, 23 offers/s | 388 (340..390) 94%, 1,484 calls/s, 29 offers/s | 386 (354..391) 94%, 1,522 calls/s, 28 offers/s | 373 (356..407) 90%, 994 calls/s, 26 offers/s |
| rust-o1000 | 417 (372..424) 100%, 0 calls/s, 0 offers/s | 395 (347..403) 95%, 2,032 calls/s, 7 offers/s | 401 (353..412) 96%, 1,513 calls/s, 5 offers/s | 402 (349..407) 96%, 1,026 calls/s, 4 offers/s | 399 (360..402) 95%, 1,426 calls/s, 6 offers/s |
| rust-o5000 | 389 (357..405) 100%, 0 calls/s, 0 offers/s | 382 (347..388) 98%, 2,005 calls/s, 0 offers/s | 372 (369..401) 96%, 1,175 calls/s, 0 offers/s | 369 (365..390) 95%, 1,501 calls/s, 0 offers/s | 384 (340..394) 99%, 1,523 calls/s, 0 offers/s |
| rust-oinf | 358 (357..368) 100%, 0 calls/s, 0 offers/s | 357 (345..369) 100%, 2,004 calls/s, 0 offers/s | 352 (341..376) 98%, 1,116 calls/s, 0 offers/s | 351 (347..370) 98%, 968 calls/s, 0 offers/s | 357 (354..363) 99%, 1,384 calls/s, 0 offers/s |
| rust-onone | 423 (382..424) 100%, 0 calls/s, 0 offers/s | 400 (377..407) 95%, 1,985 calls/s, 0 offers/s | 393 (381..403) 93%, 1,288 calls/s, 0 offers/s | 395 (376..404) 93%, 1,436 calls/s, 0 offers/s | 404 (372..405) 96%, 1,499 calls/s, 0 offers/s |

### python / process

| variant | idle | 100/s | 300/s | 1000/s | ceiling |
|---|---|---|---|---|---|
| python | 2,793 (2,723..2,865) 100%, 0 calls/s | 2,631 (2,525..2,662) 94%, 1,987 calls/s | 2,472 (2,314..2,498) 89%, 6,011 calls/s | 1,759 (1,650..1,774) 63%, 20,011 calls/s | 1,344 (1,316..1,357) 48%, 22,472 calls/s |
| rust-o0 | 2,843 (2,794..2,857) 100%, 0 calls/s, 0 offers/s | 2,665 (2,615..2,724) 94%, 1,998 calls/s, 2,740 offers/s | 2,617 (2,538..2,677) 92%, 5,991 calls/s, 2,948 offers/s | 2,254 (2,240..2,417) 79%, 20,006 calls/s, 3,655 offers/s | 1,806 (1,781..1,855) 64%, 38,682 calls/s, 3,093 offers/s |
| rust-o20 | 2,812 (2,753..2,833) 100%, 0 calls/s, 0 offers/s | 2,636 (2,550..2,727) 94%, 2,000 calls/s, 1,646 offers/s | 2,603 (2,465..2,704) 93%, 5,999 calls/s, 2,227 offers/s | 2,377 (2,173..2,411) 85%, 20,009 calls/s, 3,108 offers/s | 1,855 (1,773..1,885) 66%, 43,961 calls/s, 2,455 offers/s |
| rust-o100 | 2,850 (2,737..2,911) 100%, 0 calls/s, 0 offers/s | 2,679 (2,663..2,787) 94%, 2,000 calls/s, 148 offers/s | 2,626 (2,461..2,685) 92%, 5,999 calls/s, 626 offers/s | 2,367 (2,223..2,429) 83%, 20,001 calls/s, 1,513 offers/s | 1,826 (1,818..1,869) 64%, 40,420 calls/s, 1,507 offers/s |
| rust-o300 | 2,775 (2,736..2,806) 100%, 0 calls/s, 0 offers/s | 2,632 (2,591..2,657) 95%, 2,005 calls/s, 3 offers/s | 2,616 (2,466..2,637) 94%, 6,007 calls/s, 13 offers/s | 2,252 (2,215..2,333) 81%, 19,951 calls/s, 112 offers/s | 1,673 (1,618..1,694) 60%, 40,871 calls/s, 754 offers/s |
| rust-o1000 | 2,879 (2,866..2,881) 100%, 0 calls/s, 0 offers/s | 2,779 (2,764..2,807) 97%, 1,999 calls/s, 0 offers/s | 2,688 (2,632..2,697) 93%, 5,998 calls/s, 0 offers/s | 2,353 (2,299..2,398) 82%, 20,029 calls/s, 3 offers/s | 1,707 (1,659..1,718) 59%, 40,917 calls/s, 72 offers/s |
| rust-o5000 | 2,820 (2,712..2,854) 100%, 0 calls/s, 0 offers/s | 2,640 (2,629..2,698) 94%, 1,999 calls/s, 0 offers/s | 2,707 (2,564..2,708) 96%, 6,018 calls/s, 0 offers/s | 2,370 (2,276..2,374) 84%, 20,073 calls/s, 0 offers/s | 1,714 (1,665..1,736) 61%, 40,329 calls/s, 0 offers/s |
| rust-oinf | 2,802 (2,798..2,842) 100%, 0 calls/s, 0 offers/s | 2,752 (2,616..2,784) 98%, 1,994 calls/s, 0 offers/s | 2,708 (2,703..2,761) 97%, 5,995 calls/s, 0 offers/s | 2,513 (2,348..2,558) 90%, 19,996 calls/s, 0 offers/s | 1,838 (1,761..1,857) 66%, 42,459 calls/s, 0 offers/s |
| rust-onone | 2,808 (2,749..2,868) 100%, 0 calls/s, 0 offers/s | 2,752 (2,720..2,789) 98%, 2,004 calls/s, 0 offers/s | 2,656 (2,569..2,682) 95%, 5,989 calls/s, 0 offers/s | 2,371 (2,154..2,442) 84%, 19,989 calls/s, 0 offers/s | 1,733 (1,650..1,747) 62%, 41,762 calls/s, 0 offers/s |

### The dispatcher alone

Calls/s median (min..max), busy median, GIL offers/s median (Rust core).

| work / isolation | variant | 100/s | 300/s | 1000/s | ceiling |
|---|---|---|---|---|---|
| torch / thread | python | 2,000 (1,991..2,008) (busy 0.15) | 6,020 (5,966..6,021) (busy 0.30) | 19,993 (19,987..20,065) (busy 0.65) | 30,672 (29,154..30,693) (busy 0.82) |
| torch / thread | rust-o0 | 1,999 (1,991..2,000) (busy 0.09), 2,740 offers/s | 5,983 (5,982..6,012) (busy 0.18), 2,934 offers/s | 19,994 (19,956..20,029) (busy 0.45), 3,639 offers/s | 53,195 (49,589..57,315) (busy 0.90), 4,302 offers/s |
| torch / thread | rust-o20 | 2,007 (1,998..2,015) (busy 0.09), 1,453 offers/s | 6,006 (5,974..6,014) (busy 0.18), 2,157 offers/s | 19,943 (19,923..20,027) (busy 0.45), 3,076 offers/s | 52,697 (51,677..55,762) (busy 0.90), 2,973 offers/s |
| torch / thread | rust-o100 | 1,999 (1,999..2,008) (busy 0.10), 279 offers/s | 6,005 (5,998..6,005) (busy 0.19), 665 offers/s | 20,002 (19,961..20,011) (busy 0.48), 1,573 offers/s | 51,680 (51,172..53,216) (busy 0.87), 1,922 offers/s |
| torch / thread | rust-o300 | 1,999 (1,991..2,008) (busy 0.08), 9 offers/s | 5,982 (5,975..5,998) (busy 0.17), 30 offers/s | 19,981 (19,940..20,039) (busy 0.36), 199 offers/s | 52,172 (50,103..56,780) (busy 0.92), 988 offers/s |
| torch / thread | rust-o1000 | 1,991 (1,991..1,992) (busy 0.11), 0 offers/s | 6,005 (5,991..6,005) (busy 0.17), 0 offers/s | 20,028 (19,939..20,051) (busy 0.34), 6 offers/s | 50,139 (50,133..54,748) (busy 0.68), 101 offers/s |
| torch / thread | rust-o5000 | 2,000 (2,000..2,008) (busy 0.08), 0 offers/s | 5,998 (5,991..6,006) (busy 0.15), 0 offers/s | 20,025 (19,976..20,042) (busy 0.31), 0 offers/s | 55,267 (54,222..56,281) (busy 0.69), 0 offers/s |
| torch / thread | rust-oinf | 1,999 (1,999..2,000) (busy 0.10), 0 offers/s | 6,020 (6,014..6,036) (busy 0.17), 0 offers/s | 20,017 (19,968..20,094) (busy 0.32), 0 offers/s | 53,188 (51,153..56,793) (busy 0.67), 0 offers/s |
| torch / thread | rust-onone | 1,999 (1,991..2,000) (busy 0.08), 0 offers/s | 6,015 (6,007..6,023) (busy 0.15), 0 offers/s | 19,994 (19,921..20,025) (busy 0.30), 0 offers/s | 55,772 (54,229..56,244) (busy 0.70), 0 offers/s |
| torch / process | python | 1,992 (1,991..1,999) (busy 0.15) | 5,991 (5,990..5,998) (busy 0.30) | 19,975 (19,929..20,074) (busy 0.65) | 29,644 (29,141..30,677) (busy 0.83) |
| torch / process | rust-o0 | 1,991 (1,991..2,007) (busy 0.10), 2,739 offers/s | 5,999 (5,999..6,006) (busy 0.18), 2,940 offers/s | 20,011 (19,957..20,040) (busy 0.48), 3,648 offers/s | 52,163 (50,147..55,271) (busy 0.93), 4,239 offers/s |
| torch / process | rust-o20 | 2,007 (2,000..2,008) (busy 0.09), 1,534 offers/s | 6,014 (5,974..6,023) (busy 0.17), 2,072 offers/s | 20,028 (19,926..20,051) (busy 0.48), 3,089 offers/s | 54,254 (52,188..54,751) (busy 0.93), 3,118 offers/s |
| torch / process | rust-o100 | 1,991 (1,991..1,999) (busy 0.11), 330 offers/s | 6,015 (6,013..6,023) (busy 0.23), 892 offers/s | 20,013 (19,978..20,105) (busy 0.48), 1,576 offers/s | 54,199 (51,140..54,234) (busy 0.89), 1,983 offers/s |
| torch / process | rust-o300 | 1,999 (1,991..2,008) (busy 0.08), 7 offers/s | 6,015 (6,006..6,015) (busy 0.15), 31 offers/s | 19,979 (19,925..19,998) (busy 0.33), 153 offers/s | 54,228 (53,709..55,748) (busy 0.92), 1,017 offers/s |
| torch / process | rust-o1000 | 2,007 (1,999..2,008) (busy 0.09), 0 offers/s | 6,006 (5,999..6,021) (busy 0.17), 0 offers/s | 19,963 (19,956..19,980) (busy 0.36), 4 offers/s | 52,679 (51,623..54,218) (busy 0.70), 103 offers/s |
| torch / process | rust-o5000 | 2,007 (2,000..2,015) (busy 0.08), 0 offers/s | 6,006 (6,006..6,014) (busy 0.14), 0 offers/s | 19,977 (19,969..19,993) (busy 0.32), 0 offers/s | 51,664 (51,663..54,734) (busy 0.69), 0 offers/s |
| torch / process | rust-oinf | 2,006 (1,999..2,006) (busy 0.10), 0 offers/s | 5,982 (5,981..6,006) (busy 0.18), 0 offers/s | 19,967 (19,964..20,014) (busy 0.33), 0 offers/s | 50,639 (48,580..53,723) (busy 0.67), 0 offers/s |
| torch / process | rust-onone | 1,992 (1,991..1,999) (busy 0.09), 0 offers/s | 6,006 (6,006..6,006) (busy 0.16), 0 offers/s | 19,981 (19,952..19,991) (busy 0.33), 0 offers/s | 51,655 (51,135..54,228) (busy 0.69), 0 offers/s |
| python / thread | python | 2,000 (1,991..2,015) (busy 0.17) | 6,012 (6,006..6,014) (busy 0.31) | 20,033 (19,992..20,067) (busy 0.65) | 30,688 (30,674..31,126) (busy 0.82) |
| python / thread | rust-o0 | 1,999 (1,999..2,008) (busy 0.09), 2,740 offers/s | 5,998 (5,982..5,999) (busy 0.20), 2,956 offers/s | 19,950 (19,945..20,058) (busy 0.47), 3,632 offers/s | 50,600 (50,141..58,300) (busy 0.91), 4,079 offers/s |
| python / thread | rust-o20 | 2,000 (1,997..2,000) (busy 0.08), 1,489 offers/s | 5,999 (5,998..6,029) (busy 0.16), 2,103 offers/s | 20,035 (19,935..20,047) (busy 0.45), 3,084 offers/s | 55,797 (54,748..56,305) (busy 0.93), 3,125 offers/s |
| python / thread | rust-o100 | 2,000 (1,992..2,000) (busy 0.09), 246 offers/s | 6,015 (5,990..6,030) (busy 0.17), 663 offers/s | 20,042 (19,951..20,064) (busy 0.45), 1,542 offers/s | 54,697 (54,248..55,234) (busy 0.86), 2,005 offers/s |
| python / thread | rust-o300 | 2,000 (1,991..2,007) (busy 0.08), 4 offers/s | 5,990 (5,990..6,017) (busy 0.16), 27 offers/s | 19,969 (19,968..19,995) (busy 0.36), 177 offers/s | 50,609 (48,088..55,261) (busy 0.91), 1,015 offers/s |
| python / thread | rust-o1000 | 2,007 (2,000..2,007) (busy 0.08), 0 offers/s | 5,990 (5,983..6,014) (busy 0.16), 0 offers/s | 19,987 (19,962..19,988) (busy 0.33), 6 offers/s | 54,243 (53,187..55,254) (busy 0.69), 104 offers/s |
| python / thread | rust-o5000 | 2,008 (1,999..2,008) (busy 0.08), 0 offers/s | 5,991 (5,982..6,015) (busy 0.14), 0 offers/s | 19,949 (19,928..20,049) (busy 0.31), 0 offers/s | 55,272 (51,153..56,271) (busy 0.70), 0 offers/s |
| python / thread | rust-oinf | 2,000 (1,999..2,007) (busy 0.10), 0 offers/s | 5,983 (5,974..6,006) (busy 0.16), 0 offers/s | 19,992 (19,963..20,087) (busy 0.33), 0 offers/s | 52,177 (50,121..53,179) (busy 0.65), 0 offers/s |
| python / thread | rust-onone | 2,000 (1,999..2,000) (busy 0.08), 0 offers/s | 5,990 (5,990..6,006) (busy 0.14), 0 offers/s | 19,997 (19,899..20,027) (busy 0.30), 0 offers/s | 55,781 (54,220..56,281) (busy 0.69), 0 offers/s |
| python / process | python | 1,999 (1,999..2,007) (busy 0.19) | 5,989 (5,980..6,013) (busy 0.32) | 19,963 (19,929..20,046) (busy 0.65) | 29,144 (28,117..30,157) (busy 0.81) |
| python / process | rust-o0 | 1,999 (1,992..2,007) (busy 0.11), 2,739 offers/s | 6,006 (5,997..6,006) (busy 0.21), 2,940 offers/s | 20,000 (19,995..20,035) (busy 0.48), 3,642 offers/s | 50,059 (49,568..55,765) (busy 0.90), 4,047 offers/s |
| python / process | rust-o20 | 1,992 (1,991..1,999) (busy 0.08), 1,431 offers/s | 5,991 (5,972..5,999) (busy 0.20), 2,277 offers/s | 19,993 (19,968..20,058) (busy 0.47), 3,080 offers/s | 54,717 (51,607..57,815) (busy 0.90), 3,063 offers/s |
| python / process | rust-o100 | 1,992 (1,990..2,008) (busy 0.10), 258 offers/s | 5,998 (5,967..6,014) (busy 0.18), 627 offers/s | 19,983 (19,962..20,020) (busy 0.48), 1,559 offers/s | 55,249 (53,169..56,788) (busy 0.92), 2,029 offers/s |
| python / process | rust-o300 | 2,000 (1,990..2,007) (busy 0.09), 10 offers/s | 5,989 (5,975..5,998) (busy 0.17), 26 offers/s | 19,986 (19,964..20,002) (busy 0.33), 143 offers/s | 53,167 (50,113..54,167) (busy 0.87), 981 offers/s |
| python / process | rust-o1000 | 1,992 (1,991..1,999) (busy 0.09), 0 offers/s | 5,983 (5,983..6,014) (busy 0.16), 0 offers/s | 20,050 (19,937..20,096) (busy 0.32), 4 offers/s | 52,687 (51,669..52,699) (busy 0.70), 103 offers/s |
| python / process | rust-o5000 | 2,000 (1,991..2,000) (busy 0.08), 0 offers/s | 5,995 (5,981..6,029) (busy 0.17), 0 offers/s | 20,056 (20,027..20,078) (busy 0.33), 0 offers/s | 51,672 (50,116..54,225) (busy 0.65), 0 offers/s |
| python / process | rust-oinf | 1,999 (1,991..2,000) (busy 0.08), 0 offers/s | 5,998 (5,990..6,029) (busy 0.14), 0 offers/s | 20,007 (19,941..20,020) (busy 0.31), 0 offers/s | 55,239 (51,667..55,762) (busy 0.70), 0 offers/s |
| python / process | rust-onone | 2,000 (1,999..2,007) (busy 0.09), 0 offers/s | 5,991 (5,990..6,005) (busy 0.16), 0 offers/s | 19,998 (19,978..20,033) (busy 0.32), 0 offers/s | 53,708 (51,141..58,314) (busy 0.67), 0 offers/s |
