# Inspector

`Inspector` in `src/togeari/plugins/serve/inspector.py` is a builtin plugin that serves the
query and command tables and the event stream over local HTTP, on a `ThreadingHTTPServer`
thread (`togeari-inspector`). Requests reach the machine only through `ctx.inspect` and
`ctx.command`, so the dispatcher answers them between cycles.

## Plugin

| Attribute | Value |
|---|---|
| `name`, `priority`, `events` | `"inspector"`, 90, `"all"` |
| arguments | `host="127.0.0.1"`, `port=0` (a free port), `timeout_s=30.0`, `stream_queue=1000`, `token=GENERATE` (a random `secrets.token_urlsafe(24)`; `None` turns the check off), `allowed_hosts=()`, `max_body=1 << 20` (bytes of a command body) |
| `on_load` / `on_unload` | start / shut down the server (an IPv6 `host` binds an IPv6 server); `port` and `url` give the bound address; an IPv6 host appears in brackets (`http://[::1]:<port>`); `access_url` is `url` with `?token=` added when a token is required |
| `runtime_services()` | `{"inspector": self}` |
| `contribute_queries()` | `inspector`: `url`, open `streams`, `dropped` events, `token_required` |
| `contribute_commands()` | `inspector_end_streams`: marks every open stream ended (`end_streams`); each stream's handler leaves its loop at its next event or keepalive (within a second) and closes; answers the count marked. `inspector_token` with `token` (a non-empty string or `None`): replaces `token` for every later request (`set_token`); answers `{"token_required": bool}`, never the token |
| `on_event(event)` | puts the event on every open stream queue; a full queue drops it and counts `dropped` |

## Routes

| Route | Answer |
|---|---|
| `GET /` | `{"queries": [...], "commands": [...]}` |
| `GET /queries`, `GET /commands` | the sorted names |
| `GET /queries/<name>?arg=value` | the query's value as JSON; argument values are parsed as JSON, else kept as strings |
| `POST /commands/<name>` | the command's value as JSON; the body is a JSON object of arguments |
| `GET /events` | server-sent events: `event: <kind>` / `data: <json>` per event, `: keepalive` after a second without one |

| Status | When |
|---|---|
| 403 | access refused (below) |
| 404 | unknown route or name |
| 415 | a command body not sent as `application/json` |
| 400 | `Content-Length` is not an integer ≥ 0, the body is not a JSON object, or the query or command raised |
| 413 | a command body above `max_body` bytes |
| 504 | no answer within `timeout_s` |

Errors answer `{"error": message}`.

## Access

Every request is checked before it is served (`_refused`):

1. The `Host` header must be one of `hosts()`: `localhost`, `127.0.0.1`, `[::1]`, the bound
   host and each of `allowed_hosts`, each with the bound port.
2. An `Origin` header, when present, must be `http://` followed by one of those.
3. Unless `token` is `None`, the request must carry it as `Authorization: Bearer <token>`, an
   `X-Togeari-Token` header or a `token` query argument (`_same`: `hmac.compare_digest` over
   the UTF-8 bytes, so any characters compare and a mismatch answers 403).

A command additionally needs `Content-Type: application/json` and a `Content-Length` of an
integer from 0 to `max_body`; the length is parsed once (`_length`). A refused request is
answered from its headers (`_refuse`): its body is drained unparsed when it is at most
`DRAIN_BYTES` (64 KiB), so the connection closes cleanly; a larger one is left unread and the
connection dropped.
