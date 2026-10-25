# The dashboard

A machine with the `telemetry` plugin writes its records, rollups, traces and sampled series
into a directory; `togeari dash` serves a page over every such directory, and while a machine
runs with the `inspector` plugin the page also reads it and changes it through its inspector.

```python
from togeari.plugins import Inspector
from togeari.plugins.telemetry import Telemetry

m = Machine(..., plugins=[Telemetry("runs/caption-0", inspector_token=True), Inspector()])
```

```
togeari dash runs/            # http://127.0.0.1:8787
```

`togeari dash` needs the `telemetry` extra (numpy, kohakuvault). It is a process of its own and
opens each run directory read-only; a run is any directory holding `machine.json` under the
roots, found again on every listing.

## Runs and their state

| State | Meaning |
|---|---|
| live | `machine.json` has no stop time and the inspector it names answers (probed with a 1 s timeout) |
| unreachable | no stop time, and no inspector answers: a machine killed before it stopped, or run without an inspector |
| stopped | `machine.json` has a stop time (written when the telemetry plugin unloads) |

Recorded data is the same in every state. The page's views (overview, program, machine, jobs,
timeline) read it for the chosen range.

## A live machine

While the open run is live, the page reads the machine once a second through the server's
proxy (`/api/run/<id>/live/...` to the inspector, which carries the token): `stats`, `lanes`,
`knob_specs`, `jobs` and `plugins`, not while the page is hidden. It also keeps the last 200
of the machine's rare events from the inspector's event stream (`unit.*`, `lane.dead`,
`plugin.disabled`, `snapshot.error`, `fault.taken`, `machine.*`), listed on the overview and
drawn as rules on the timeline. The inspector subscribes to the bus only while a stream is
open, and only to those kinds.

Every change goes through a confirm step that shows the old value, the new one and the command
sent, then the machine's answer or its error:

| Where | Change | Command |
|---|---|---|
| Machine → dispatcher | a knob, with an editor by its kind (`knob_specs`) | `set_knob` |
| Machine → dispatcher | a plugin on or off | `plugin_enable`, `plugin_disable` |
| Machine → a lane | its former or selector, for a contributed policy | `set_lane_policy` |
| Machine → a unit | detach it, draining what it holds | `detach` |
| Jobs | a resident job: pause, resume, checkpoint, cancel | `pause_job`, `resume_job`, `checkpoint_job`, `cancel_job` |

Policies are offered by name: a plugin's `contribute_policies()` names them, and
`knob_specs` lists them by the roles they fill. Disabling the inspector closes its server, so
the page loses the machine; disabling telemetry closes its writer.

## Access

The inspector's token reaches the dash server through `machine.json` when the telemetry plugin
has `inspector_token=True` (or `togeari dash --token`); the server never sends it to the page.
Anyone who can read the run directory can drive the machine.

The server accepts a request only when its `Host` names the server (a loopback name, the bound
host, or an `--allow-host` name) with its port, and an `Origin`, when present, is `http://`
such a host; a POST body must be `application/json`. Another page open in the same browser
therefore cannot send commands through the proxy (a cross-origin JSON POST needs a preflight
the server never grants), nor read the API by pointing a name of its own at the loopback
address. Reaching the page under another name (a tunnel, a reverse proxy) takes
`--allow-host name` or `--allow-host name:port`.

## Working on the page

The app is `apps/dashboard` (Vue, uPlot, the KIR graph library); `npm run build` writes it into
`src/togeari/dash/static`, which `togeari dash` serves.

```
python apps/dashboard/scripts/record_runs.py apps/dashboard/dev-runs          # recorded runs
python apps/dashboard/scripts/record_runs.py apps/dashboard/dev-runs --live   # a live machine
togeari dash apps/dashboard/dev-runs
cd apps/dashboard && npm run dev           # http://localhost:5190, /api proxied to :8787
npm run shots -- <out dir> [filter]        # every view, both themes, recorded and live
```

The live dev machine runs until stopped, with an outage of one VLM unit for 10 s of every 40 s
and contributed policies to swap. The dev server forwards its own origin to the dash server as
the server's.
