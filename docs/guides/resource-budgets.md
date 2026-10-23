# Resource-budget batching and admission

`BudgetFormer` batches compatible items using additive estimates in arbitrary named dimensions,
such as bytes, tokens, pixels, or expected work. It is not a physical memory allocator or a
reservation system. Estimates must come from cheap existing inputs/metadata: do not build a
payload, call an adapter, perform I/O, or synchronize a device inside an estimator.

## Configuration

```python
from togeari.policies import BudgetFormer, ResourcePressure


def estimate(item):
    return {"bytes": item.inputs["estimated_bytes"], "work": item.inputs["estimated_work"]}


former = BudgetFormer(
    budgets={"bytes": 8_000_000, "work": 1_000},
    estimate=estimate,
    size=32,
    wait_ms=5,
    oversized="reject",
    estimator_revision="request-costs/1",
)
admission = ResourcePressure("work-lane", limits={"bytes": 32_000_000}, max_jobs=128)
```

Budgets and admission limits are nonempty mappings with finite positive values. Every
configured cost dimension must be present and finite/nonnegative; missing means unknown, not
zero. Booleans, negative values, nonfinite values, overflow, and positive values that underflow
to zero are rejected. Values are normalized to binary floating-point estimates; batch fitting
sums those normalized values exactly so small charges cannot vanish beside large ones.

Configurations and captured estimates are immutable. Replace a former to change its budgets,
estimator, revision, size, or wait. A new former has a distinct private estimator generation;
that local generation is not a portable execution identity.

## Formation and progress

The original engine compatibility key is preserved. `Bucketed(former, key=...)` can split
those compatible buckets further; resource budgets never combine incompatible keys.

Readiness follows count-or-wait summaries. Once a bucket is ripe, selection walks its
selector-eligible priority/FIFO prefix, up to the former count and unit batch limits. It stops
at the first eligible item that does not fit every dimension. It does not skip that item to
pack smaller younger items. The former returns the exact selected items, not a count that a
later filter could substitute with unbudgeted items.

`wait_ms` is a batching delay, not a dispatch guarantee: unavailable units, exhausted capacity,
selector rejection, or older/higher-priority work can still delay execution. Progress assumes
responsive eligible units and finite work ahead; existing priority semantics do not promise
fairness under perpetual higher-priority arrivals.

## Invalid estimates and oversized items

- Invalid or raising estimators yield terminal `ItemError(EstimateError(...))`.
- Default `oversized="reject"` yields `ItemError(ResourceBudgetExceeded(...))` when one item
  exceeds a budget.
- Explicit `oversized="singleton"` permits that item alone as a reported **soft-budget
  overrun**. It never shares that batch with other items.

A lane must validate estimates before queueing and fail closed. Resource-policy errors must
not use a generic former-error fallback that submits a full batch. Estimates are cached per
item and generation, not by input equality, and retries normally reuse them. When a plugin
transforms submission inputs, the lane re-estimates as necessary and checks the final batch
again before actual submission; a previously valid batch is not authorization to bypass the
budget after transformation.

## Admission and accounting

Lane resource signals distinguish queued, retry-held, and inflight estimated costs and their
total. Transfers between these states move a charge instead of duplicating it. Queue routing
or rebucketing does not create another logical charge. Unknown estimates and explicit
oversized singleton submissions are reported separately. Accounting sums normalized costs
exactly internally. If an aggregate exceeds the finite floating-point reporting range, its
reported total saturates at the largest finite float and the dimension appears in
`overflow_dimensions`; it is a flagged lower bound, not an exact total or an unknown estimate.
Reports never require JSON infinity. As work drains, exact internal accounting determines
when the overflow flag clears and normal reporting resumes.

`ResourcePressure` reads the lane's estimated total and admits a **new job** only while each
configured dimension is below its limit and the job cap permits it. It never gates resident
work that may release pressure. Construction checks maintained `ResourcePressure` rules,
including exact `All(...)` composition: the lane must exist, use a supported `BudgetFormer`,
and estimate every configured admission dimension. Typos fail before units start rather than
silently preventing every job from entering. Arbitrary custom admission policies are not
introspected. Initial known zero totals allow admission; missing lanes,
missing dimensions, malformed totals, or unknown estimates refuse admission. Its
`status(pool, signals)` method returns the reason, such as `missing_lane`, `missing_dimension`,
`unknown_resource_estimates`, `resource_limit`, or `max_jobs`, so a configuration error need
not look like unexplained waiting. Overflow of a configured dimension refuses admission as
`resource_limit` with `overflow=True`, rather than disguising known excessive pressure as an
invalid or unknown estimate. Overflow in an unrelated dimension does not by itself block a
policy that limits other dimensions. Removing or changing the resource former later can
make those signals unavailable; admission then remains blocked with the explicit status
reason while resident jobs can still drain.

There is no prediction of a future job's entire resource demand, no physical reservation, and
no implication that cancellation immediately releases external device memory. Engine-provided
actual load metrics remain separate from these estimates.

## Execution identity

Budget rejection and estimation errors are **observable execution policy**, not merely
advisory batching preferences. The execution manifest includes normalized budgets, oversized
behavior, the error-contract version, and `estimator_revision`. An absent estimator revision
is explicitly unknown unless a valid whole-`binding_revision` attestation covers it.
Changing the budget always changes identity even under an unchanged bundle attestation.
Replacing the estimator callable invalidates its old bundle coverage and marks active-job
history as changed, even when the descriptor previously had no estimator revision. That
coverage does not silently return after swapping the callable back. A new machine/bundle or
explicit estimator revision supplies the declaration for future jobs; an unchanged explicit
revision is the author's equivalence assertion, not an automatic fingerprint of Python code.

`size`, `wait_ms`, and estimator-generation tokens are not semantic identity. They may still
affect timing and external observations; neither singleton mode nor this classification
promises identical fresh stochastic outcomes. Resource descriptors are exposed separately
from advisory former descriptions. Arbitrary custom former behavior is not automatically
certified harmless or covered by the maintained `BudgetFormer` contract.
