/**
 * Rollup series downsampled in the page: the server sends each series' finest stored rows
 * (`/fine`, float32 columns on a dense grid), the page buckets them at the step it chooses.
 *
 * A bucket's rate is its count over the seconds of it inside `clip` (the run and the range),
 * so a bucket the run only partly covers (the run's first, the one still filling) reads the
 * same rate as a whole one.
 */

/** Seconds a bucket may span, finest first. */
export const STEPS = [
  1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400,
]

/** The least of `STEPS` giving at most `points` buckets over `seconds`. */
export function stepFor(seconds, points) {
  const want = seconds / Math.max(1, points)
  return STEPS.find((s) => s >= want) ?? STEPS[STEPS.length - 1]
}

/** `/fine`'s bytes as `{ t0, t1, columns, series: [[{tier, period, start, len, n, sum, ...}]] }`. */
export function decodeFine(buf) {
  const view = new DataView(buf)
  const size = view.getUint32(0, true)
  const head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, size)))
  let at = 4 + size
  const series = head.series.map((pieces) =>
    pieces.map((p) => {
      const out = { ...p }
      for (const c of head.columns) {
        out[c] = new Float32Array(buf, at, p.len)
        at += p.len * 4
      }
      return out
    }),
  )
  return { t0: head.t0, t1: head.t1, columns: head.columns, series }
}

/** The rows of `piece` from `a` (inclusive) to `b` (exclusive), as a piece. */
function cut(piece, columns, a, b) {
  const i = Math.max(0, Math.ceil((a - piece.start) / piece.period))
  const j = Math.min(piece.len, Math.ceil((b - piece.start) / piece.period))
  if (j <= i) return null
  const out = { ...piece, start: piece.start + i * piece.period, len: j - i }
  for (const c of columns) out[c] = piece[c].subarray(i, j)
  return out
}

/**
 * `held` (a result for an earlier range) with `fresh` (read from `fresh.t0` on) appended:
 * the held rows inside `[t0, fresh.t0)` kept, the fresh ones after them.
 */
export function mergeFine(held, fresh, t0) {
  if (!held || held.series.length !== fresh.series.length) return fresh
  const series = fresh.series.map((pieces, k) => [
    ...held.series[k].map((p) => cut(p, fresh.columns, t0, fresh.t0)).filter(Boolean),
    ...pieces,
  ])
  return { ...fresh, t0: Math.min(t0, fresh.t0), series }
}

/** Where a held result's rows end: the start of its last row (read again by a refresh). */
export function lastRow(fine) {
  let at = -Infinity
  for (const pieces of fine?.series || []) {
    const p = pieces[pieces.length - 1]
    if (p?.len) at = Math.max(at, p.start + (p.len - 1) * p.period)
  }
  return Number.isFinite(at) ? at : null
}

/**
 * One series' pieces bucketed at `step` seconds (at least each piece's row period), buckets
 * at multiples of it: `{t, v, lo, hi}`, `t` the bucket's start (the clip's start for the
 * first). `stat`: "rate" (count over covered seconds; `lo` / `hi` the least and greatest
 * row rate inside the bucket), "count", "sum", "mean", "min" or "max" (`lo` / `hi` the
 * values' range). Only rows inside `clip` count, each for the seconds of it inside.
 */
export function bucket(pieces, { stat = "rate", step = 1, clip = [-Infinity, Infinity] } = {}) {
  const t = []
  const v = []
  const lo = []
  const hi = []
  const [c0, c1] = clip
  // A bucket is keyed by its start, so one split across pieces (a refresh's) stays one
  let key = null
  let n = 0
  let sum = 0
  let cov = 0
  let a = Infinity
  let b = -Infinity
  const flush = () => {
    if (key === null || cov <= 0) return
    t.push(Math.max(key, c0))
    if (stat === "rate") v.push(n / cov)
    else if (stat === "count") v.push(n)
    else if (stat === "sum") v.push(sum)
    else if (stat === "mean") v.push(n ? sum / n : null)
    else if (stat === "min") v.push(Number.isFinite(a) ? a : null)
    else if (stat === "max") v.push(Number.isFinite(b) ? b : null)
    lo.push(Number.isFinite(a) ? a : null)
    hi.push(Number.isFinite(b) ? b : null)
  }
  for (const p of pieces || []) {
    const eff = Math.ceil(step / p.period) * p.period
    for (let i = 0; i < p.len; i++) {
      const s = p.start + i * p.period
      const covered = Math.min(s + p.period, c1) - Math.max(s, c0)
      if (covered <= 0) continue
      const k = Math.floor(s / eff) * eff
      if (k !== key) {
        flush()
        key = k
        n = sum = cov = 0
        a = Infinity
        b = -Infinity
      }
      const rn = p.n[i]
      n += rn
      cov += covered
      if (p.sum) sum += p.sum[i]
      if (stat === "rate") {
        const r = rn / covered
        if (r < a) a = r
        if (r > b) b = r
      } else if (p.lo && rn > 0) {
        if (p.lo[i] < a) a = p.lo[i]
        if (p.hi[i] > b) b = p.hi[i]
      }
    }
  }
  flush()
  return { t, v, lo, hi }
}
