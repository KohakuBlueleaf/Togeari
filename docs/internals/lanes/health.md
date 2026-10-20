# Unit health

Unit states, health polls, retries, timeouts, the dead-lane limit and elastic units of a `Lane`
(`Health` in `src/togeari/core/lanes/health.py`). `ItemError` never changes a unit's state;
`UnitError` takes the unit out of service until `ready()` reports true again.

## States

`State` holds the names.

| State | Gets new work | Entered by |
|---|---|---|
| `up` | yes | construction; `attach` with `ready()` true; a successful health poll |
| `down` | no | `unit_down` (a `UnitError` in a submission); `attach` with `ready()` false |
| `draining` | no; in-flight items finish | `detach(unit, drain=True)`; `detach(unit, drain=False)` with a submission holding an `ordered` item |
| `detached` | no | `detach(unit, drain=False)`; a draining unit whose last item settled |

Every change goes through `_set_state`, which clears the units-up cache, bumps `up_version` and
sets `_pump_needed`. Units are started before they join a lane (at construction, or by the
machine off the dispatcher thread for `attach`), so a lane never holds a unit still starting.

## Down and back up

`unit_down(unit, err, now)` (only from `up`):

- `down`; `next_poll = now + (err.retry_after or backoff)`; `backoff` doubles up to
  `POLL_MAX_S = 30` (it starts at `POLL_FIRST_S = 0.5`);
- `errors[kind]`, `stats["failovers"]`;
- the unit's own queue moves to the shared queue;
- `unit.down`.

`_poll(st, now)`, from `timers` for `down` units past `next_poll`: `ready()` true puts the unit
`up` (backoff reset, `stats["recoveries"]`, `unit.up` with reason `ready`); false or raising
schedules the next poll after the doubled backoff.

## Timers

`timers(now)`, called every cycle after the pump, returns `(item, result)` pairs to deliver:

1. Health polls (above).
2. Held retries whose backoff ended are re-queued at the front.
3. Item timeouts past their deadline, while still applying:
   - armed at enqueue (no submission): the item leaves the lane with
     `ItemError(ItemTimeout("... past its deadline while queued"))`;
   - armed at submit: `ItemError(ItemTimeout("... over Ns"))`; once every item of the submission
     has settled, `unit.cancel(ticket)` and the submission closes.
4. Dead lane: once no unit has been up for `lane_dead_s`, every queued and held item fails with
   `ItemError(UnitUnavailable)`, `stats["dead_failed"]` counts them, and `lane.dead` is emitted
   with the count; items queued later fail at the next call while no unit is up.

## Retry

Each `UnitError` on an item counts `ext["retries"]` and holds it for `retry.backoff_ms` before
it is re-queued at the front. At `retry.max`, or at once for an `ordered` node that is not
`idempotent` (`ext["ordered_once"]`), the item fails with `UnitUnavailable`, a catchable fault
for its job. The unit goes down before the retries are held, so they go to other units.

## Elastic units

`attach(unit)` takes a started unit:

- a new unit: refused (`ValueError`) when another unit of the lane has its name; a detached unit
  with that name is forgotten; the unit joins (`_join`: `units`, spec, batch cap, the shared
  queue's key unit, engine identity, its wake);
- a detached unit joins again; a draining one is taken back;
- a unit already in the lane is refused;
- `up` when `ready()` is true, else `down` and polled with backoff; `unit.attach`.

`detach(unit, drain, now)`:

- the unit's own queue moves to the shared queue;
- `drain=True`: `draining`, `unit.detach` (reason `drain`); it becomes detached when its last
  in-flight item settles;
- `drain=False`: every open submission is cancelled on the unit and closed, and its unsettled,
  unsquashed items are re-queued at the front, except a submission holding an `ordered` item
  (`ext["ordered_once"]`), which finishes on the unit; `unit.detach` (reason `now`); detached at
  once, or `draining` while such a submission runs.

Becoming detached (`_detached`) removes the unit from `units` (`_leave`, which recomputes the
batch cap and engine identity; the `UnitState` stays for views and a later attach) and emits
`unit.detached` with the reason (`drained` or `now`). The machine then stops the unit off the
dispatcher thread when no other lane holds it ([machine](../construct/machine.md)).
