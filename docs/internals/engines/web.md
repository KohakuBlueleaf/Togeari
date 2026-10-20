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
| `health_path`, `health_timeout_s` | `None`, 2 | a GET that must succeed before `ready()` is true again after a `UnitError` |
| `start_timeout_s` | 10 | the I/O loop's start |
| `pool_size` | 2 | connections per `httpx` client |
| `on_unit_error` | `"block"` | `UNIT_ERROR_MODES`: what a `UnitError` does to the other requests in flight |
| `name` | `"web"` | |

`kind = "call"`, `protocol = "http"`, `shares_memory = False`. `spec()` is the identity:
engine, name, base URL, `max_inflight`, `rate`, `timeout_s`.

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
   `UnitError` under `on_unit_error="cancel"` it also takes every other open ticket, answers
   each with the same error (`unit_error_items`), and posts a cancel of its task; under
   `"block"` the other tickets run on.

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

The engine inherits `add_wake` / `wake` from `EngineBase` but never calls `wake()`: its capacity
rises only through completions (`done`) or at a known time (`wake_at()`).

A `UnitError` sets `blocked_until = now + retry_after` and marks the service unhealthy. The lane
reads `wake_at()` while it has queued work for the unit ([lane](../lanes/lane.md)), so a rate
limit refilling wakes the dispatcher without polling. `TokenBucket`
(`engines/web/limiter.py`) is shared by the dispatcher thread (`available`, `due`) and the loop
(`acquire`), under its own lock.

## Cancel and stop

`cancel(ticket)` answers `Cancelled()` if not yet answered and cancels the request task on the
loop. `stop()` answers every open ticket with `UnitError(EngineStopped)`, then the loop cancels
its tasks and closes the clients.
