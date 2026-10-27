/**
 * The machine as one picture, from the run's sampled series over the range.
 *
 * Cumulative counters (dispatcher cycles, busy seconds, a unit's items done) give rates by
 * their growth over the range; gauges (queue, in flight, jobs by state) give a mean, the last
 * sample and a trend. A unit's fill is its held items (sent by the lane, not answered) over
 * held plus its free capacity, on every kind of lane. A stream engine that reports requests
 * waiting inside itself (`load().waiting`) adds them to the lane's queue.
 */

export const JOB_STATES = ["executing", "queued", "waiting", "parked"]

export const JOB_STATE_NOTE = {
  executing: "has statements ready or running",
  queued: "its calls wait in lanes",
  waiting: "waits on results in flight",
  parked: "a strand parked on a branch",
}

export const PHASES = [
  ["drain", "Drain", "the inbox: results, commands, queries"],
  ["fetch", "Fetch", "next statement per strand; rename into the window"],
  ["issue", "Issue", "the ready set across jobs; inline nodes run here"],
  ["pump", "Pump", "lanes form batches and pick units"],
  ["retire", "Retire", "in program order per job; results to the sink"],
  ["admit", "Admit", "new jobs from the source"],
]

/** Growth per second of a cumulative series over its samples (null with fewer than two). */
export function growth(s) {
  if (!s || s.t.length < 2) return null
  const dt = s.t[s.t.length - 1] - s.t[0]
  return dt > 0 ? (s.v[s.v.length - 1] - s.v[0]) / dt : null
}

/** Per-interval growth per second of a cumulative series: `{t, v}` one shorter. */
export function perSecond(s) {
  if (!s || s.t.length < 2) return { t: [], v: [] }
  const t = []
  const v = []
  for (let i = 1; i < s.t.length; i++) {
    const dt = s.t[i] - s.t[i - 1]
    if (dt <= 0) continue
    t.push(s.t[i])
    v.push(Math.max(0, (s.v[i] - s.v[i - 1]) / dt))
  }
  return { t, v }
}

export const mean = (s) => (s?.v.length ? s.v.reduce((a, b) => a + b, 0) / s.v.length : null)
export const last = (s) => (s?.v.length ? s.v[s.v.length - 1] : null)
/** The greatest sample: the buckets' highs when the series is bucketed. */
export const peak = (s) =>
  (s?.hi || s?.v)?.length ? (s.hi || s.v).reduce((a, b) => Math.max(a, b)) : null

/** `a + b` sample by sample (either alone when the other is missing). */
function added(a, b) {
  if (!a || !b) return a || b
  const n = Math.min(a.v.length, b.v.length)
  return { t: a.t.slice(0, n), v: a.v.slice(0, n).map((x, i) => x + b.v[i]) }
}

/** `a / (a + b)` sample by sample, on `a`'s times (b at the same index). */
function share(a, b) {
  if (!a || !b) return null
  const n = Math.min(a.v.length, b.v.length)
  const v = []
  for (let i = 0; i < n; i++) {
    const total = a.v[i] + b.v[i]
    v.push(total > 0 ? a.v[i] / total : 0)
  }
  return { t: a.t.slice(0, n), v }
}

/**
 * Everything the machine picture shows. `samples`: name -> {t, v}; `built`: the run's
 * machine record (lanes with their units, unit specs); `tables`: lane and unit tables by
 * name, entry rows.
 */
export function machineModel(samples, built, tables) {
  const S = (name) => samples?.[name] || null

  const phases = PHASES.map(([key, label, note]) => ({
    key,
    label,
    note,
    share: growth(S(`dispatcher/phase/${key}`)) ?? 0,
  }))
  const dispatcher = {
    busy: growth(S("dispatcher/busy_s")),
    busyTrend: perSecond(S("dispatcher/busy_s")),
    cycles: growth(S("dispatcher/cycles")),
    calls: growth(S("dispatcher/calls")),
    callsTrend: perSecond(S("dispatcher/calls")),
    squashed: growth(S("dispatcher/squashed")),
    phases,
    phaseTop: Math.max(1e-9, ...phases.map((p) => p.share)),
  }

  const states = JOB_STATES.map((s) => ({
    key: s,
    note: JOB_STATE_NOTE[s],
    mean: mean(S(`jobs/${s}`)),
    last: last(S(`jobs/${s}`)),
    series: S(`jobs/${s}`),
  }))
  const resident = S("jobs/resident")
  const pool = {
    states,
    mean: mean(resident),
    last: last(resident),
    peak: peak(resident),
    trend: resident?.v || [],
    series: resident,
  }

  const entries = tables.entry || []
  const sum = (k, f) => entries.reduce((n, e) => n + (e.metrics[k]?.[f] ?? 0), 0)
  const main = [...entries].sort(
    (a, b) => (b.metrics.admitted?.n ?? 0) - (a.metrics.admitted?.n ?? 0),
  )[0]
  const jobs = {
    admitted: sum("admitted", "n"),
    admittedRate: sum("admitted", "rate"),
    done: sum("job", "n"),
    doneRate: sum("job", "rate"),
    failed: sum("failed", "n"),
    p50: main?.metrics.job?.p50 ?? null,
    p90: main?.metrics.job?.p90 ?? null,
    entries: entries.length,
  }

  const specs = built?.units || {}
  const admission = built?.knobs?.admission ?? null
  const lanes = (built?.lanes || []).map((l) => {
    const stream = l.kind === "stream"
    const units = l.units.map((name) => unitModel(name, S, specs[name], tables.unit?.get(name)))
    const engineWaiting = S(`lane/${l.name}/waiting`)
    const inflight = S(`lane/${l.name}/inflight`)
    const queue = S(`lane/${l.name}/queue`)
    const cap = units.reduce((n, u) => n + (u.capacity ?? 0), 0)
    const t = tables.lane?.get(l.name) || {}
    return {
      name: l.name,
      kind: l.kind,
      stream,
      gatesAdmission: admission?.lane === l.name,
      config: l.config,
      units,
      queue: { mean: mean(queue), last: last(queue), peak: peak(queue), series: queue },
      engineQueue: {
        mean: mean(engineWaiting),
        last: last(engineWaiting),
        peak: peak(engineWaiting),
        series: engineWaiting,
      },
      waiting: {
        mean: (mean(queue) ?? 0) + (mean(engineWaiting) ?? 0),
        series: added(queue, engineWaiting),
      },
      inflight: { mean: mean(inflight), last: last(inflight), series: inflight },
      unitsUp: last(S(`lane/${l.name}/units_up`)),
      capacity: cap || null,
      fill: cap ? (mean(inflight) ?? 0) / cap : null,
      items: t.service?.rate ?? null,
      batch: t.batch?.mean ?? null,
      queueP90: t.queue?.p90 ?? null,
      serviceP90: t.service?.p90 ?? null,
      submits: growth(S(`lane/${l.name}/submitted`)),
      retries: growth(S(`lane/${l.name}/retries`)),
      errors: units.reduce((n, u) => n + u.errors, 0),
    }
  })
  return { dispatcher, pool, jobs, lanes, admission }
}

function unitModel(name, S, spec, table) {
  const p = `unit/${name}/`
  const inflight = S(`${p}held`)
  const running = S(`${p}running`)
  const waiting = S(`${p}waiting`)
  const fill = share(inflight, S(`${p}free`))
  const caps = []
  const free = S(`${p}free`)
  if (inflight && free)
    for (let i = 0; i < Math.min(inflight.v.length, free.v.length); i++)
      caps.push(inflight.v[i] + free.v[i])
  const errors = table?.errors?.n ?? 0
  const outages = last(S(`${p}outage_rejects`)) ?? 0
  const items = growth(S(`${p}done`))
  const state = outages > 0 ? "down" : errors ? "errors" : items ? "up" : "idle"
  return {
    name,
    spec: spec ? [spec.engine, spec.kind].filter(Boolean).join(" · ") : "",
    inflight: { mean: mean(inflight), last: last(inflight), series: inflight },
    running: { mean: mean(running), peak: peak(running), series: running },
    waiting: { mean: mean(waiting), peak: peak(waiting), series: waiting },
    capacity: caps.length ? Math.max(...caps) : null,
    fill: mean(fill),
    fillSeries: fill,
    busy: growth(S(`${p}busy_s`)),
    items,
    batch: table?.batch?.mean ?? null,
    serviceP50: table?.service?.p50 ?? null,
    serviceP90: table?.service?.p90 ?? null,
    errors,
    outages,
    state,
  }
}

export const UNIT_STATE = {
  up: { chip: "chip-sage", icon: "i-carbon-checkmark-filled", text: "serving" },
  errors: { chip: "chip-amber", icon: "i-carbon-warning-alt-filled", text: "errors" },
  down: { chip: "chip-coral", icon: "i-carbon-error-filled", text: "had outages" },
  idle: { chip: "chip-warm", icon: "i-carbon-circle-dash", text: "idle" },
}

/** A policy as the machine record describes it (`{type, ...settings}`), or a plain value. */
export function policyText(v) {
  if (v === null || v === undefined) return null
  if (typeof v !== "object")
    return String(v).replace(/^<(?:[\w.]+\.)?(\w+) object at 0x[0-9A-Fa-f]+>$/, "$1")
  if (!v.type) return JSON.stringify(v)
  const args = Object.entries(v)
    .filter(([k]) => k !== "type")
    .map(([k, x]) => `${k} ${x}`)
  return args.length ? `${v.type} (${args.join(", ")})` : v.type
}

/** A lane's set config as short chips. */
export function configChips(config) {
  if (!config || typeof config !== "object") return []
  return Object.entries(config)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k}: ${policyText(v)}`)
}
