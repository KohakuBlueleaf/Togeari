/**
 * The dashboard server's API (`togeari dash`, src/togeari/dash/server.py).
 *
 * Entities are "kind:id" strings (node:<addr>, lane:<id>, unit:<id>, entry:<addr>); times are
 * unix seconds; every call returns parsed JSON or throws with the server's error.
 */

import { decodeFine, lastRow, mergeFine } from "@/utils/fine"

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
  for (const [k, v] of Object.entries(params))
    if (v !== undefined && v !== null) q.set(k, String(v))
  return q.size ? `?${q}` : ""
}

async function get(path, params = {}) {
  const url = `/api/${path}${query(params)}`
  return parsed(await fetch(url), url)
}

async function post(path, body) {
  const url = `/api/${path}`
  return parsed(
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
    url,
  )
}

const run = (id, path, params) => get(`run/${encodeURIComponent(id)}/${path}`, params)

/**
 * The finest rows of `pairs` (`[[entity, metric], ...]`) over `[t0, t1)`, decoded
 * (`decodeFine`); `columns` among n, sum, lo, hi. A `refresh` of `previous` (a result of the
 * same pairs) reads only from its last row on and appends.
 */
async function fine(id, pairs, { t0, t1, columns = ["n"], signal, previous, refresh = false }) {
  const since = refresh ? lastRow(previous) : null
  const params = {
    pairs: pairs.map(([e, m]) => `${e}/${m}`).join(","),
    t0,
    t1,
    since,
    columns: columns.join(","),
  }
  const url = `/api/run/${encodeURIComponent(id)}/fine${query(params)}`
  const r = await fetch(url, { signal })
  if (!r.ok) await parsed(r, url)
  const got = decodeFine(await r.arrayBuffer())
  return since === null ? got : mergeFine(previous, got, t0)
}

/**
 * One series per entity of `entities`, bucketed by the server at `step` seconds (quantiles:
 * merged histograms), read together. A `refresh` of `previous` (a result of the same query at
 * the same step) reads only from its last bucket on and appends.
 */
async function seriesEach(id, entities, metric, { t0, t1, step, stat, signal, previous, refresh }) {
  const held =
    refresh && previous?.length === entities.length && previous.every((s) => s.step === step)
  const since = held
    ? Math.max(...previous.map((s) => (s.t.length ? s.t[s.t.length - 1] : -Infinity)))
    : null
  const params = { entities: entities.join(","), metric, t0, t1, step, stat }
  if (Number.isFinite(since)) params.since = since
  const url = `/api/run/${encodeURIComponent(id)}/series_each${query(params)}`
  const got = await parsed(await fetch(url, { signal }), url)
  if (params.since === undefined) return got
  return got.map((s, k) => {
    const p = previous[k]
    const a = lowerBound(p.t, Math.floor(t0 / step) * step)
    const b = lowerBound(p.t, since)
    const out = { ...s, step }
    for (const c of ["t", "v", "lo", "hi"]) out[c] = p[c].slice(a, b).concat(s[c])
    return out
  })
}

/** Buckets per sampled series a view asks for (`sampleStream`), whatever the range. */
export const SAMPLE_POINTS = 1000

/** The JSON lines of a response body, each as it arrives. */
async function* lines(r) {
  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ""
  for (;;) {
    const { done, value } = await reader.read()
    buf += decoder.decode(value, { stream: !done })
    let at
    while ((at = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, at)
      buf = buf.slice(at + 1)
      if (line) yield JSON.parse(line)
    }
    if (done) break
  }
  if (buf.trim()) yield JSON.parse(buf)
}

/** The first index of ascending `xs` whose value is not below `x`. */
function lowerBound(xs, x) {
  let lo = 0
  let hi = xs.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (xs[mid] < x) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** Where a `sampleStream` result keeps its bucket step and the start of its last bucket. */
const BUCKETS = Symbol("buckets")

/**
 * `{name: {t, v, lo, hi, unit}}` from the head and the chunks so far (newest first), over the
 * buckets of `previous` the chunks do not reach (mean time inside the range, before `from`).
 */
function mergedSamples(head, chunks, previous, from) {
  const out = {}
  let last = -Infinity
  for (const [name, unit] of Object.entries(head.units)) {
    // Oldest first: the previous points in [t0, from) (their times ascend), then the chunks
    const parts = { t: [], v: [], lo: [], hi: [] }
    const p = previous?.[name]
    if (p?.t?.length) {
      const a = lowerBound(p.t, head.t0)
      const b = lowerBound(p.t, from)
      if (b > a) {
        parts.t.push(p.t.slice(a, b))
        parts.v.push(p.v.slice(a, b))
        parts.lo.push((p.lo || p.v).slice(a, b))
        parts.hi.push((p.hi || p.v).slice(a, b))
      }
    }
    for (let c = chunks.length - 1; c >= 0; c--) {
      const x = chunks[c][name]
      if (x) for (const k of ["t", "v", "lo", "hi"]) parts[k].push(x[k])
    }
    const s = { unit }
    for (const k of ["t", "v", "lo", "hi"])
      s[k] = parts[k].length === 1 ? parts[k][0] : [].concat(...parts[k])
    if (s.t.length) last = Math.max(last, Math.floor(s.t[s.t.length - 1] / head.step) * head.step)
    out[name] = s
  }
  // Configurable and writable: a reactive proxy may only wrap such a property's value
  Object.defineProperty(out, BUCKETS, {
    value: { step: head.step, last },
    configurable: true,
    writable: true,
  })
  return out
}

/**
 * Sampled series bucketed to at most `points` per series, read newest first: `onPart` gets the
 * merged series after each chunk (`previous` filling the time not read yet), the promise the
 * whole range. A `refresh` of `previous` (a result of the same query) reads only from its last
 * bucket on when the step is unchanged.
 */
async function sampleStream(
  id,
  { names, prefix, t0, t1, points = SAMPLE_POINTS, chunks = 8 },
  { signal, previous, onPart, refresh = false } = {},
) {
  const held = refresh ? previous?.[BUCKETS] : null
  const since = held && Number.isFinite(held.last) ? held.last : null
  const params = {
    names: names?.join(","),
    prefix: prefix?.join(","),
    t0,
    t1,
    points,
    chunks,
    step: since === null ? null : held.step,
    since,
  }
  const url = `/api/run/${encodeURIComponent(id)}/samples${query(params)}`
  const r = await fetch(url, { signal })
  if (!r.ok) await parsed(r, url)
  let head = null
  let from = Infinity
  const parts = []
  for await (const msg of lines(r)) {
    if (msg.error) throw new Error(msg.error)
    if (!head) {
      head = msg
      continue
    }
    parts.push(msg.series)
    from = Math.min(from, msg.from)
    onPart?.(mergedSamples(head, parts, previous, from))
  }
  if (!head) return {}
  // A refresh keeps the buckets before `since`; a full read keeps none of `previous`
  return head.since > head.t0
    ? mergedSamples(head, parts, previous, head.since)
    : mergedSamples(head, parts, null, -Infinity)
}
const livePath = (id, path) => `run/${encodeURIComponent(id)}/live/${path}`

/**
 * A live run's machine, through the server's proxy to its inspector. Query arguments are sent as
 * JSON (the inspector parses each value as JSON, else keeps the string).
 */
const live = {
  query: (id, name, args = {}) =>
    get(
      livePath(id, `queries/${name}`),
      Object.fromEntries(Object.entries(args).map(([k, v]) => [k, JSON.stringify(v)])),
    ),
  command: (id, name, body = {}) => post(livePath(id, `commands/${name}`), body),
  /** An EventSource of the machine's events of `kinds` (every kind when empty). */
  events: (id, kinds = []) =>
    new EventSource(`/api/${livePath(id, "events")}${query({ kinds: kinds.join(",") || null })}`),
}

export const api = {
  runs: () => get("runs"),
  state: (id) => run(id, "state"),
  info: (id) => run(id, "info"),
  programs: (id) => run(id, "programs"),
  program: (id, start) => run(id, `program/${start}`),
  table: (id, kind, t0, t1) => run(id, "table", { kind, t0, t1 }),
  series: (id, entity, metric, t0, t1, points = 600, stat = "rate") =>
    run(id, "series", { entity, metric, t0, t1, points, stat }),
  seriesEach,
  fine,
  top: (id, kind, metric, t0, t1, stat = "p90", k = 10) =>
    run(id, "top", { kind, metric, t0, t1, stat, k }),
  hist: (id, entity, metric, t0, t1) => run(id, "hist", { entity, metric, t0, t1 }),
  samples: (id, { names, prefix, t0, t1 }) =>
    run(id, "samples", { names: names?.join(","), prefix, t0, t1 }),
  sampleStream,
  traces: (id, t0, t1, reason, limit = 200) => run(id, "traces", { t0, t1, reason, limit }),
  trace: (id, key) => run(id, "trace", { key }),
  live,
}
