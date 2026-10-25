/**
 * Statement metrics joined to the program: the `table?kind=node` rows (one per code address)
 * with each address's statement (op, node, graph path, source line), and grouped by graph path
 * for the graph overlay (a path's addresses are one element on the graph).
 */

/** addr -> statement, over every program. */
export function statementIndex(programs) {
  const out = new Map()
  for (const p of programs) for (const s of p.statements || []) out.set(s.addr, { ...s, program: p.start })
  return out
}

const VALUED = ["wait", "queue", "service"]

/** One row per statement with calls, errors, faults and the three phases. */
export function statementRows(table, index) {
  return (table || []).map((r) => {
    const m = r.metrics
    const s = index.get(r.id) || {}
    const row = {
      entity: r.entity,
      addr: r.id,
      node: s.node || s.op || `@${r.id}`,
      op: s.op,
      path: s.path,
      line: s.line,
      program: s.program,
      calls: m.calls?.n ?? 0,
      rate: m.calls?.rate ?? 0,
      errors: m.errors?.n ?? 0,
      faults: m.faults?.n ?? 0,
      cancelled: m.cancelled?.n ?? 0,
    }
    for (const k of VALUED) row[k] = m[k] || null
    return row
  })
}

/** The node of a source graph frame a lowered statement belongs to: the frame is entered
 * through the call sites `prefix` (node ids, outermost first); the statement's chain is its
 * origin's call sites then its own node. Inside the frame it is the chain's next element (its
 * own node, or the call it was inlined through); null outside the frame. */
export function frameKey(origin, prefix) {
  if (!origin) return null
  const chain = [...(origin.calls || []).map((c) => c.node_id), origin.node_id]
  if (chain.length <= prefix.length) return null
  for (let i = 0; i < prefix.length; i++) if (chain[i] !== prefix[i]) return null
  return chain[prefix.length]
}

/** Mean µs a call spends in a phase (0 without values). */
export const mean = (row, k) => row[k]?.mean ?? 0

/** Total µs a statement's calls spent in `phases` (calls × each phase's mean). */
export function totalTime(row, phases = VALUED) {
  return phases.reduce((t, k) => t + (row[k]?.n ?? 0) * mean(row, k), 0)
}

/** Rows grouped by graph path: summed counts, call-weighted phase means, the worst p90s, and
 * the time spent in `phases`. */
export function byPath(rows, phases = VALUED) {
  const out = new Map()
  for (const r of rows) {
    if (!r.path) continue
    const g = out.get(r.path) || { path: r.path, node: r.node, addrs: [], calls: 0, rate: 0, errors: 0, faults: 0, cancelled: 0, time: 0 }
    g.addrs.push(r.addr)
    for (const k of ["calls", "rate", "errors", "faults", "cancelled"]) g[k] += r[k]
    g.time += totalTime(r, phases)
    for (const k of VALUED) {
      const v = r[k]
      if (!v) continue
      const cur = g[k] || { n: 0, sum: 0, p90: 0, p99: 0, max: 0 }
      cur.n += v.n
      cur.sum += v.mean * v.n
      cur.p90 = Math.max(cur.p90, v.p90 ?? 0)
      cur.p99 = Math.max(cur.p99, v.p99 ?? 0)
      cur.max = Math.max(cur.max, v.max ?? 0)
      cur.mean = cur.sum / cur.n
      g[k] = cur
    }
    out.set(r.path, g)
  }
  return out
}
