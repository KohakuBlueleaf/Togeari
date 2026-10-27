/**
 * A kept job trace (its raw records) as waterfall rows: one row per statement run, with its
 * wait, queue and service segments in µs from the job's admission.
 *
 * Record times are when each record was written: `enqueue` at issue (queued = the wait),
 * `result` at the result (queued = lane queue, service = unit time), `inline` after the run
 * (queued = the wait, service = the run), `hit` at the store hit (queued = the wait). A
 * statement's enqueue and result are paired per address in order.
 */
export function waterfall(trace, index, laneName, unitName) {
  const recs = trace?.records || []
  const admit = recs.find((r) => r.kind === "admit")
  const end = recs.find((r) => r.kind === "end")
  const t0 = admit ? admit.t : (recs[0]?.t ?? 0)
  const at = (t) => (t - t0) * 1e6
  const rows = []
  const open = new Map()
  const marks = []
  const label = (addr) => {
    const s = index.get(addr)
    return s ? s.node || s.op : `@${addr}`
  }

  for (const r of recs) {
    switch (r.kind) {
      case "enqueue": {
        const issue = at(r.t)
        const row = {
          addr: r.addr,
          node: label(r.addr),
          lane: laneName(r.lane),
          segs: [],
          start: issue - (r.queued_us ?? 0),
        }
        if (r.queued_us) row.segs.push({ phase: "wait", a: issue - r.queued_us, b: issue })
        rows.push(row)
        if (!open.has(r.addr)) open.set(r.addr, [])
        open.get(r.addr).push(row)
        break
      }
      case "result": {
        const row = open.get(r.addr)?.shift()
        if (!row) break
        const done = at(r.t)
        const svc = r.service_us ?? 0
        const q = r.queued_us ?? 0
        if (q) row.segs.push({ phase: "queue", a: done - svc - q, b: done - svc })
        if (svc) row.segs.push({ phase: "service", a: done - svc, b: done })
        row.unit = r.unit ? unitName(r.unit) : null
        row.status = r.status
        row.end = done
        break
      }
      case "inline":
      case "hit": {
        const done = at(r.t)
        const svc = r.service_us ?? 0
        const wait = r.queued_us ?? 0
        const segs = []
        if (wait) segs.push({ phase: "wait", a: done - svc - wait, b: done - svc })
        if (svc) segs.push({ phase: "service", a: done - svc, b: done })
        rows.push({
          addr: r.addr,
          node: label(r.addr),
          lane: r.kind === "hit" ? "store hit" : "inline",
          segs,
          start: done - svc - wait,
          end: done,
          status: r.status,
        })
        break
      }
      case "fault":
      case "park":
      case "resume":
        marks.push({
          kind: r.kind,
          t: at(r.t),
          addr: r.addr,
          node: r.addr !== null ? label(r.addr) : "",
        })
        break
    }
  }
  for (const row of rows)
    if (row.end === undefined) row.end = Math.max(row.start, ...row.segs.map((s) => s.b))
  rows.sort((a, b) => a.start - b.start)
  return { rows, marks, total: end ? at(end.t) : Math.max(0, ...rows.map((r) => r.end)) }
}
