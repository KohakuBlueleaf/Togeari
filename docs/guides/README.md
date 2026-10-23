# Guides

For contributors: [paired KIR/Togeari validation and release order](paired-validation.md).

| Guide | For |
|---|---|
| [Writing a machine](writing-a-machine.md) | programs, nodes, bindings, lanes, policies, knobs; running, submitting, inspecting, stopping |
| [Writing an engine](writing-an-engine.md) | the engine contract, a minimal call engine, the behaviour rules and `check_engine`, wakes, batch engines and adapters |
| [Writing a plugin](writing-a-plugin.md) | hooks and their semantics, errors, gates, events, queries and commands, builtin plugins, the inspector |
| [Builtin engines](builtin-engines.md) | `PythonCallEngine`, `WebCallEngine` with `JsonHttp`, `TorchEngine` with `TorchModule` and `TorchAdapter` |
| [Stores, sources and sinks](stores-and-io.md) | the store contract, `MemoryStore`, `VaultStore`, writing a store; sources, sinks, sharding, job keys |
| [Tuning](tuning.md) | admission, window, issue order, lane options, effects, events, the collector, the core and `gil_offer_us`; reading the run report and trace |
| [Modules and origins](modules-and-origins.md) | importable KIR subgraphs, dependency identity, source attribution, portable replay archives |
| [Typed records](records.md) | immutable schemas and values, ordinary worker calls, explicit codec boundaries, storage and replay |
| [Ordered collection](collect.md) | native result-producing iteration, carried state, precise faults, mutation restriction and fallback replay |
| [Execution policy](execution-policy.md) | explicit call deadlines, lane attempt limits, retry scope, failure causes and legacy timeout-hint migration |
| [Execution identity](execution-identity.md) | declared revisions, manifest completeness, persistence compatibility, current reports versus initial archived identity, and replay limits |
| [Invocation identity](invocation-identity.md) | logical job runs, dynamic invocations, execution attempts, snapshot continuity and engine metadata |
| [Result reuse](result-reuse.md) | deterministic eligibility, content v2 and invocation-safe positional v3 keys, live configuration transitions and migration |
| [Requested checkpoints](checkpoints.md) | coherent retirement cuts, pinned artifacts, asynchronous persistence and explicit compatible resume |
| [Resource budgets](resource-budgets.md) | additive budget-aware batches, cheap estimates, lifecycle accounting and new-job backpressure |
| [Explaining waits](explaining-waits.md) | bounded causal snapshots linking input, control, effect, lane and checkpoint waits to source origins |
| [Committed events](committed-events.md) | ordered semantic emit, bounded durable outbox, at-least-once delivery and event-sidecar conformance |
| [Conformance](conformance.md) | recording, `conform()` in replay and re-execute modes, `togeari conform`, reading records with `Archive`, `togeari doctor` |
| [Examples](examples.md) | what each example shows and how to run it |

The ideas behind them are in [concepts](../concepts/README.md); how each part is built is in
[internals](../internals/README.md).
