# Run-report policy views

`plugins/report/spec.py` writes the explanatory construction snapshot in
`spec.json`. Existing program, binding, lane, unit, policy and knob fields remain
available. The following additions expose resolved execution policy separately
from scheduling preferences; they are **not an execution manifest or a complete
execution identity**.

## Effective call rows

Each `programs[region].calls` list contains one row per bound call instruction:

| Field | Meaning |
|---|---|
| `addr`, `node` | decoded instruction address and node spelling |
| `origin` | structured source provenance, including imported definition and call ancestry |
| `pattern` | binding pattern actually selected for this instruction |
| `inline` | whether execution is on the dispatcher |
| `lane` | resolved lane name, or `null` for inline calls |
| `execution_policy.source` | `"binding"`, `"legacy_hint"`, or `"none"` |
| `execution_policy.timeout_s` | effective per-call timeout, or `null` for no call deadline |

These rows use the already-resolved `Bound` values, not a fresh interpretation of
hints or the binding configuration. Inline calls have no call timeout; ignored
legacy inline timeout hints remain visible in construction warnings. An explicit
empty `ExecutionPolicy()` has source `"binding"` and timeout `null`, and overrides
a legacy timeout hint just as an explicit numeric timeout does. Zero is retained
as zero, not treated as an absent timeout.

`bindings[pattern].policy` separately describes the supplied configuration:
`null` means no explicit policy, while `{"timeout_s": null}` means an explicit
empty policy. Binding configuration is fixed for a machine. Mutating `m.bind` or
a `Use` object is **not a supported rebind** of loaded regions; such mutation may
make the configuration description differ from the effective call rows. The rows
continue to report what is bound. Documented lane-policy commands are distinct
from program rebinding.

## Lane policy groups

Lane rows retain their existing flat fields and add:

- `observable_policy`: `timeout_s`, `retry` (type and primitive attributes), and
  `lane_dead_s`. Timeouts, retries, and availability failures affect observable
  execution outcomes.
- `advisory`: `former`, `select`, `forming`, and `max_inflight`. These describe
  scheduling preferences and performance configuration; they do not promise
  stochastic timing invariance or make cancellation modes merely advisory.

Per-call and lane timeouts are not collapsed into one reported number: their
clock origins differ, and runtime enforcement combines their deadlines. This
snapshot does not predict a job's future deadline, retry outcome, or cancellation.

The Inspector's [`programs` query](inspector.md#effective-call-policy) exposes the
same detached effective call rows. Reading these views performs no execution and
changes no policy.
