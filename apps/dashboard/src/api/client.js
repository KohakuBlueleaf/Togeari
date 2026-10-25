/**
 * The dashboard server's API (`togeari dash`, src/togeari/dash/server.py).
 *
 * Entities are "kind:id" strings (node:<addr>, lane:<id>, unit:<id>, entry:<addr>); times are
 * unix seconds; every call returns parsed JSON or throws with the server's error.
 */

async function parsed(r, url) {
  const text = await r.text()
  let body
  try {
    body = JSON.parse(text)
  } catch {
    throw new Error(`${r.status} ${url}: not JSON (${text.slice(0, 80)})`)
  }
  if (!r.ok) throw new Error(body.error || `${r.status} ${url}`)
  return body
}

function query(params) {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null) q.set(k, String(v))
  return q.size ? `?${q}` : ""
}

async function get(path, params = {}) {
  const url = `/api/${path}${query(params)}`
  return parsed(await fetch(url), url)
}

async function post(path, body) {
  const url = `/api/${path}`
  return parsed(await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }), url)
}

const run = (id, path, params) => get(`run/${encodeURIComponent(id)}/${path}`, params)
const livePath = (id, path) => `run/${encodeURIComponent(id)}/live/${path}`

/**
 * A live run's machine, through the server's proxy to its inspector. Query arguments are sent as
 * JSON (the inspector parses each value as JSON, else keeps the string).
 */
const live = {
  query: (id, name, args = {}) => get(livePath(id, `queries/${name}`), Object.fromEntries(Object.entries(args).map(([k, v]) => [k, JSON.stringify(v)]))),
  command: (id, name, body = {}) => post(livePath(id, `commands/${name}`), body),
  /** An EventSource of the machine's events of `kinds` (every kind when empty). */
  events: (id, kinds = []) => new EventSource(`/api/${livePath(id, "events")}${query({ kinds: kinds.join(",") || null })}`),
}

export const api = {
  runs: () => get("runs"),
  state: (id) => run(id, "state"),
  info: (id) => run(id, "info"),
  programs: (id) => run(id, "programs"),
  program: (id, start) => run(id, `program/${start}`),
  table: (id, kind, t0, t1) => run(id, "table", { kind, t0, t1 }),
  series: (id, entity, metric, t0, t1, points = 600, stat = "rate") => run(id, "series", { entity, metric, t0, t1, points, stat }),
  top: (id, kind, metric, t0, t1, stat = "p90", k = 10) => run(id, "top", { kind, metric, t0, t1, stat, k }),
  hist: (id, entity, metric, t0, t1) => run(id, "hist", { entity, metric, t0, t1 }),
  samples: (id, { names, prefix, t0, t1 }) => run(id, "samples", { names: names?.join(","), prefix, t0, t1 }),
  traces: (id, t0, t1, reason, limit = 200) => run(id, "traces", { t0, t1, reason, limit }),
  trace: (id, key) => run(id, "trace", { key }),
  live,
}
