# Togeari documentation

| Section | What it holds |
|---|---|
| [concepts/](concepts/README.md) | what the machine is: jobs as sequential runs, the CPU picture, strands, engines and lanes, plugins, the conformance proof |
| [guides/](guides/README.md) | how to use it: writing a machine, an engine, a plugin; the builtin engines; stores, sources and sinks; tuning; conformance; the examples |
| [internals/](internals/README.md) | how it is built, file by file: code memory, the dispatcher, lanes, policies, plugins, verification, the native core |
| [performance/](performance/README.md) | measurements; [the cores compared](performance/native.md) |

Read [concepts](concepts/README.md) first, then [writing a machine](guides/writing-a-machine.md).

The dispatcher has two implementations of one protocol: the Python core under
`src/togeari/core/`, the reference, and the Rust core `togeari._rs`, which every wheel carries.
A machine runs the Rust core by default (`core="auto"`) and the Python core when the extension
is not built; `Machine(core="python" | "rust")` or `TOGEARI_CORE` picks one. Both make the same
decisions in the same order ([parity](internals/native/parity.md)); the Rust core is 1.9–5.1×
faster at the dispatcher ceiling, and its knob `gil_offer_us` trades dispatcher speed against
how often other threads get the GIL ([tuning](guides/tuning.md#the-native-core)).
