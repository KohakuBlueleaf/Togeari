# Web call engine

`WebCallEngine` (`src/togeari/engines/web/engine.py`) is a call engine for any HTTP service. One
asyncio I/O thread sends the requests through pooled `httpx` clients; the item's adapter (for
example `JsonHttp`) builds the request and decodes the response.

## Arguments

| Argument | Default | Meaning |
|---|---|---|
| `base_url` | — | the service |
| `max_inflight` | 64 | requests in flight at once |
| `rate` | `None` | token bucket: `(requests per second, burst)`, or a number (burst 1) |
| `headers` | `{}` | sent with every request |
| `timeout_s` | 600 | per request (`httpx.Timeout`) |
| `connect_timeout_s` | 10 | the connect part of `httpx.Timeout`; `ConnectTimeout` classifies as `UnitError` |
| `load_probe` | `None` | a function polled every `probe_interval_s` (default 1) in a thread; its dict is reported in `load()` |
| `classify` | `default_classify` | outcome to result class |
| `health_path`, `health_timeout_s` | `None`, 2 | a GET that must succeed before `ready()` is true again after a `UnitError`; it goes through its own one-connection client |
| `start_timeout_s` | 10 | the I/O loop's start; past it the loop is told to close and `start` raises |
| `pool_size` | 2 | connections per `httpx` client |
| `on_unit_error` | `"block"` | `UNIT_ERROR_MODES`: what a `UnitError` does to the other requests in flight |
| `max_retry_after_s` | 3600 | the longest block a `Retry-After` sets; an infinite or larger value blocks this long |
| `name` | `"web"` | |

`kind = "call"`, `protocol = "http"`, `shares_memory = False`. `spec()` is the identity:
engine, name, base URL, `max_inflight`, `rate`, `timeout_s`. The other arguments (`headers`,
`connect_timeout_s`, `pool_size`, `on_unit_error`, ...) are not identity: two engines that
differ only in them share content-store keys.

`start()` moves the state `STOPPED` → `STARTING` → `UP` under the lock, so a second `start`
while one runs does nothing and one I/O loop exists per start. It clears a pending health probe
left by the previous run.

## Request path

1. `submit(work, done)`: a `Ticket`; `inflight += 1`; with a bucket the ticket is "unpaced"
   until it takes a token. The launch is queued for the I/O loop (`_post`); one
   `call_soon_threadsafe` wake-up serves every queued call.
2. On the loop: `_launch` creates a task (`ticket.handle`), which takes a connection (`_acquire`:
   a free connection, else the least loaded client), waits for a token, builds
   `item.payload()` (an `HttpRequest`) and sends it.
3. `classify(response or exception)`: a result class, or `None` to answer
   `finish(item, response)`.
4. `_settle`: frees the slot, records latency (EWMA) and errors, answers the ticket. For a
   `UnitError` under `on_unit_error="cancel"` it also answers every other unanswered open
   ticket with the same error (`unit_error_items`); their requests run on, keep their slots,
   and their late answers are dropped. Under `"block"` the other tickets run on and answer
   normally.

Connections are split by `pool_sizes(max_inflight, pool_size)`: `ceil(max_inflight / pool_size)`
clients sharing `max_inflight` connections as evenly as possible.

`default_classify` (`engines/web/classify.py`):

| Outcome | Result |
|---|---|
| status 429 or 503 | `UnitError(HTTPStatusError, retry_after)` from the `Retry-After` header (seconds or HTTP date) |
| other status ≥ 400 | `ItemError(HTTPStatusError)` |
| `ConnectError`, `ConnectTimeout` | `UnitError` |
| other exception | `ItemError` |
| other response | `None` (decode it) |

## Capacity, pacing and health

| Method | Gives |
|---|---|
| `capacity()` | 0 unless up and past `blocked_until`; `max_inflight - inflight`, and 0 while the bucket holds less than one token beyond the unpaced tickets |
| `wake_at()` | the earliest of the `Retry-After` block end and the time the bucket will hold one token more than the unpaced tickets; `None` when neither applies |
| `load()` | `running` (in flight), and in `extra` the counters, `latency_ms`, errors by kind, the last probe |
| `ready()` | false while stopped or before `blocked_until`; with `health_path`, the last health result, a new GET started in the background when none is pending |

Capacity rises through completions (`done`), at a known time (`wake_at()`), or when `_settle`
frees the slot of a ticket already answered (cancelled, or answered by a unit error under
`"cancel"`): no `done` reaches the lane then, so `_settle` calls `wake()` (`EngineBase`).

A `UnitError` sets `blocked_until = now + min(retry_after, max_retry_after_s)` and marks the
service unhealthy. The health probe clears its pending flag in a `finally`, so a probe cancelled
by `stop` does not keep later probes from starting. The lane
reads `wake_at()` while it has queued work for the unit ([lane](../lanes/lane.md)), so a rate
limit refilling wakes the dispatcher without polling. `TokenBucket`
(`engines/web/limiter.py`) is shared by the dispatcher thread (`available`, `due`) and the loop
(`acquire`), under its own lock.

## Cancel and stop

`cancel(ticket)` answers `Cancelled()` if not yet answered. The request task is never aborted:
aborting an `httpx` request while its connection opens can leave that pooled connection stuck,
and later requests then wait on it without end. A task whose ticket is already answered sends
nothing (checked before the pool and after the token wait); one already sent runs to its end,
its answer is dropped, and `_settle` frees its slot. `stop()` answers every open ticket with
`UnitError(EngineStopped)`, then the loop cancels its tasks and closes the clients.
