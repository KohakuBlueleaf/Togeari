# Guides

| Guide | For |
|---|---|
| [Writing a machine](writing-a-machine.md) | programs, nodes, bindings, lanes, policies, knobs; running, submitting, inspecting, stopping |
| [Writing an engine](writing-an-engine.md) | the engine contract, a minimal call engine, the behaviour rules and `check_engine`, wakes, batch engines and adapters |
| [Writing a plugin](writing-a-plugin.md) | hooks and their semantics, errors, gates, events, queries and commands, builtin plugins, the inspector |
| [Builtin engines](builtin-engines.md) | `PythonCallEngine`, `WebCallEngine` with `JsonHttp`, `TorchEngine` with `TorchModule` and `TorchAdapter` |
| [Stores, sources and sinks](stores-and-io.md) | the store contract, `MemoryStore`, `VaultStore`, writing a store; sources, sinks, sharding, job keys |
| [Tuning](tuning.md) | admission, window, issue order, lane options, effects, events, the collector, the core and `gil_offer_us`; reading the run report and trace |
| [Conformance](conformance.md) | recording, `conform()` in replay and re-execute modes, `togeari conform`, reading records with `Archive`, `togeari doctor` |
| [Examples](examples.md) | what each example shows and how to run it |

The ideas behind them are in [concepts](../concepts/README.md); how each part is built is in
[internals](../internals/README.md).
