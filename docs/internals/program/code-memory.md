# Code memory

Every loaded job program is decoded into a **region** of one instruction list,
`CodeMemory.mem` (`src/togeari/core/program/code.py`). A strand's `pc` is an index into it, so a
job never refers to its program except through addresses.

## Objects

| Class | Fields |
|---|---|
| `CodeMemory` | `mem: list[Instr]`; `regions: list[Region]`; `by_hash: {semantic_hash: Region}`; `entries: {name: Entry}`; `tries: {try_id: TryInfo}` (every region's) |
| `Region` | `index`; `job` (the `JobProgram`); `start`, `end` (address range); `entry_addr`; `tables` ([decode tables](decode-tables.md)); `tries` (this region's); `problems` (from decoding) |
| `Entry` | `name`; `addr` (= `region.entry_addr`); `region`; `params` and `outputs` read from `region.job` |

| Method | Does |
|---|---|
| `cached(job)` | `by_hash.get(job.semantic_hash)` |
| `stage(job)` | compiles `job.program.body` at the end of `mem` and builds `Tables`; the region is not yet known to any lookup |
| `commit(region)` | makes a staged region known: `tries`, `regions`, the sorted start list for `region_of`, `by_hash` |
| `load(job, problems)` | the cached region, or `stage` and, when it decoded without problems, `commit` |
| `add_entry(name, region)` | registers `Entry(name, region.entry_addr, region)` |
| `region_of(addr)` | binary search over committed region starts (`bisect_right`) |
| `tables(addr)` | `region_of(addr).tables` |
| `calls(region)` | every `call` instruction of the region |
| `dump(region)` | the text `togeari decode` prints |

Regions are only appended. A staged region that never commits leaves unused addresses and is
never cached. Loading while jobs run (the `load` command) never moves or rewrites an existing
instruction.

## Instructions

`Instr` (`src/togeari/core/program/decode/instr.py`) is one decoded statement; `Instr.op` is one
of the `Op` constants.

| `op` | From | Fields used | `succ()` |
|---|---|---|---|
| `call` | node call | `node`, `signature`, `args`, `outs`, `n_sig_out`, `ordered`, `mutates`, `binding`, `next` | `[next]` |
| `assign` | `x = y`, `x = literal` | `target`, `operand`, `next` | `[next]` |
| `branch` | `branch` | `operand`, `targets = [true, false]` | `targets` |
| `switch` | `switch` | `operand`, `cases = [(value, addr)]`, `default` | case addresses, then `default` (or `-1`), deduplicated |
| `jump` | `jump`, `parallel` | `targets = [addr]` | `[targets[0]]` |
| `each` | `each` | `operand` (items), `item`, `index`, `body`, `after`, `iter_end`, `has_exit` | `[body, after]` |
| `iter_end` | end of an `each` body | `each_addr` | `[each_addr]` |
| `end` | end of the job program | — | `[-1]` |

Every instruction also has `addr`, `region`, `span`, `hint` (the statement's `@hint` dict),
`each_ctx` (innermost `each` whose body holds it) and `try_id` (innermost `@try` body holding it).

**Operands** are tuples: `("n", name)` reads a name, `("l", value)` a literal, `("d", value)` a
signature default, `("missing", port)` a required input nobody gave (a runtime fault when
fetched).

**Call arguments.** `Compiler.call` matches positional arguments to `signature.inputs` in order,
then keyword arguments by port name. Every signature input gets an operand (given, `missing`, or
`d`); unknown keywords are appended. `mutates` is `[(port, name read by that port or None)]`.

`writes()` and `reads()` give the names an instruction writes and reads. A `call` with `mutates`
also writes `ALL` (`"*"`): any name may change, because aliases are not known statically.

`Span(source, line, node, def_of)` locates an instruction. `def_of` is the `@def` an inlined
statement came from, recovered from inline-renamed names (`<def>__<n>__...`).

## Compiling a body

`Compiler` (`src/togeari/core/program/decode/compiler.py`) flattens the lowered body.

- `CodeMemory.stage` gives each compile the next region index, committed or not.
- `compile(body)` first allocates the region's `end` instruction, so a region's first address is
  always its `end`. It returns the entry address.
- `block(stmts, scopes, cont, each_ctx, try_id)` walks the statements **last to first**, so
  addresses are not in program order. Each statement's continuation is the next statement's entry.
- Namespaces (`label:` blocks), `@def`s and type hints produce no instruction; a namespace is
  skipped in sequence.
- `@dataflow:` bodies are compiled inline with the dataflow statement's continuation.
- `@try` produces no instruction. Its body is compiled with a fresh `try_id = (region, n)`, its
  handler with the outer `try_id`; both continue after the `@try`.
  `TryInfo(try_id, body, handler, error_name, outer)` records the body's addresses.
- Namespace bodies continue:
  - after the namespace definition (branch and switch arms, jump targets);
  - at the next arm, or after the `parallel` statement for the last arm;
  - at the `iter_end` instruction for an `each` body. `each_body[each_addr]` is the set of the
    body's addresses plus that `iter_end`.
- Targets are resolved after the walk (`later(fix)`): branch and switch labels in the local scope,
  `jump` labels in the local scope and then the enclosing ones. An unknown label is a problem
  ``namespace `x` not found``.
- An unresolvable node is a problem `node 'x' is ...`; its `call` is emitted without a signature
  and binding reports it again.

``()parallel(`a`, `b`)`` compiles to a `jump` to arm `a`; `a` continues into `b`, and `b` after
the `parallel`. The arms run in listed order inside one strand.

## Example

```
@def (a: int, xs: list)main(r, acc):
    (a)small(c)
    (c)branch(`yes`, `no`)
    yes:
        (a)inc(t)
    no:
        t = 0
    (a)inc(u)
    acc = []
    (xs)each(x, `body`)
    body:
        (x, t)mix(v)
        (acc, v)append(acc)
    @try:
        (u)flaky(r)
    @except err:
        r = 99
```

`togeari decode demo.kir --entry main --lib kohakunode.testing:LIB` (spans trimmed):

```
region 0  demo.kir:main  hash 84581536098a7c15
entry @7  inputs ['a', 'xs']  outputs ['r', 'acc']
    0  end
    1  (u)flaky(r) -> 0                  try=(0, 1)
    2  r = 99 -> 0
    3  each xs as (x, None) body 12 end 8 after 1   reconv=1 structured may_write=['acc', 'v', 'x'] body_writes=['acc', 'v'] carried=['acc'] has_exit=False
    4  acc = [] -> 3
    5  (a)inc(u) -> 4
    6  branch c ? 9 : 10                 reconv=5 structured may_write=['t']
    7  (a)small(c) -> 6
    8  iter_end of 3                     each_ctx=3
    9  (a)inc(t) -> 5
   10  t = 0 -> 5
   11  (acc, v)append(acc) -> 8          each_ctx=3
   12  (x, t)mix(v) -> 11                each_ctx=3
try (0, 1): body 1..1 handler @2 error err
```

The entry is `@7`; the `@try` has no instruction, only `try=(0, 1)` on `@1` and the handler `@2`.
