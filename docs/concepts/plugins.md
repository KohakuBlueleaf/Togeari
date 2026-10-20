# Plugins

Plugins change or observe what happens at the seams between parts without forking them:

| Kind | Hooks | Semantics |
|---|---|---|
| transform | `pre_admit`, `pre_submit`, `post_result`, `pre_sink` | return a new value, or `None` to keep it |
| block | any `pre_*` | raise `PluginBlockError`: the operation does not happen |
| veto | `vote_admit` | return `False` to stop admitting for now |
| callback | `on_*` | fire-and-forget |
| events | `on_event` | every bus event, on the plugin's own thread |
| contribution | `contribute_*`, `runtime_services` | queries, commands, policies, services |

A plugin that raises is disabled and the machine goes on. Counters, the run report, the
trace, the conformance recorder, the HTTP inspector and the budget are ordinary plugins.

`machine.inspect(name)` and `machine.command(name)` go through the dispatcher's inbox, so they
see and change a consistent state: jobs, lanes, units, programs, knobs, statistics; attach and
detach, pause and cancel, `set_knob`, load a program, add or toggle a plugin.
