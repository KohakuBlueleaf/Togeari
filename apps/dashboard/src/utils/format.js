/** Numbers as the dashboard prints them: durations from µs, rates, counts, clock times. */

const DASH = "—"

/** A duration given in µs: "870 µs", "12.4 ms", "3.21 s", "4m 05s". */
export function us(v) {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  if (v < 1000) return `${Math.round(v)} µs`
  if (v < 1e6) return `${(v / 1e3).toFixed(v < 1e4 ? 2 : 1)} ms`
  if (v < 6e7) return `${(v / 1e6).toFixed(v < 1e7 ? 2 : 1)} s`
  const s = Math.round(v / 1e6)
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`
}

/** A count: 1234 -> "1,234", 1.2e6 -> "1.20 M". */
export function count(v) {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(2)} M`
  return Math.round(v).toLocaleString("en-US")
}

/** A rate per second with three significant digits. */
export function rate(v) {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  if (v === 0) return "0"
  if (v >= 1000) return count(v)
  return v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)
}

export function pct(v, digits = 1) {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  return `${(v * 100).toFixed(digits)}%`
}

/** A plain number with up to `digits` decimals. */
export function num(v, digits = 1) {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  return Number.isInteger(v) ? count(v) : v.toFixed(digits)
}

/** Unix seconds as a local clock time. */
export function clock(t, withDate = false) {
  if (!t) return DASH
  const d = new Date(t * 1000)
  const hms = d.toLocaleTimeString("en-GB", { hour12: false })
  return withDate ? `${d.toLocaleDateString("en-CA")} ${hms}` : hms
}

/** A span of seconds: "45 s", "12 min", "3.5 h". */
export function span(s) {
  if (!Number.isFinite(s)) return DASH
  if (s < 120) return `${Math.round(s)} s`
  if (s < 7200) return `${Math.round(s / 60)} min`
  if (s < 172800) return `${(s / 3600).toFixed(1)} h`
  return `${(s / 86400).toFixed(1)} d`
}

export { DASH }
