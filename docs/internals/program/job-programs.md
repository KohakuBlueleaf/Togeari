# Job programs

`build_job_programs` in `src/togeari/core/program/job.py` turns one KIR program and a list of
entries into one `JobProgram` per entry.

## Steps

1. `read_source(program)`: an `os.PathLike` is read from disk (source name = the path, base dir
   = its parent); a `str` is always KIR text (source name `<text>`); anything else is a problem.
2. `compile_source(text, libraries, base, sig_path, passes=[])` parses and checks. Each
   error-severity diagnostic of a `KirCheckError` becomes a problem `<source>:<line>  <code>
   <message>`.
3. Per entry:
   - `None` is the top level. A name must be a top-level `@def`.
   - `_job_program(program, entry)` deep-copies the program and builds
     `Program(uses, body=[every other @def] + entry.body, mode)`.
   - `run_passes(job, env, passes)` lowers it; `passes` defaults to `DEFAULT_PASSES`.
   - `params`: one `Param(name, has_default, default)` per `@def` parameter (a literal default is
     kept; any other default expression gives `default=None` with `has_default=True`).
   - `outputs`: the `@def`'s output names, or `None` for the top level.
   - Hashes: `semantic_hash = sha256(semantic_hash(lowered) \x1f entry \x1f ",".join(passes))`,
     and `full_hash` the same over `full_hash(lowered)`.

`DEFAULT_PASSES = ["order_dataflow", "inline", "strip_meta"]`. `expand_each`, `version` and
`group` are not in the default list; any list `run_passes` accepts may be given (`passes=`).

## `JobProgram`

| Field | Content |
|---|---|
| `source` | the path, or `<text>` |
| `entry` | the entry `@def` name, or `None` |
| `program` | the lowered job program (decoded into code memory) |
| `written` | the program as written (the reference replays this one, [conformance](../verification/conformance.md)) |
| `env` | the `NodeEnv` that resolves node names |
| `params` | `list[Param]` |
| `outputs` | output names, or `None` (every top-level source name) |
| `passes` | the pass list used |
| `semantic_hash`, `full_hash` | hex digests, entry and passes mixed in |
| `text` | the source text |
| `warnings` | the checker's warnings as strings |

`JobProgram.name` is `<source>:<entry>` (`<top>` for the top level).
