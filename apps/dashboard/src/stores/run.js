import { defineStore } from "pinia"
import { computed, ref, watch } from "vue"

import { api } from "@/api/client"

/** Range presets: seconds back from the run's last bucket; "all" is the whole run. */
export const PRESETS = [
  ["1m", 60],
  ["5m", 300],
  ["15m", 900],
  ["1h", 3600],
  ["6h", 21600],
  ["all", null],
]

export const VIEWS = [
  ["overview", "Overview", "i-carbon-dashboard"],
  ["program", "Program", "i-carbon-flow"],
  ["machine", "Machine", "i-carbon-chip"],
  ["jobs", "Jobs", "i-carbon-list-boxes"],
  ["timeline", "Timeline", "i-carbon-chart-line"],
]

const LIVE_POLL_MS = 2000

/**
 * The run on screen and the time range every view reads.
 *
 * `range` is a preset (seconds back from the end of the run, null for all of it) or a brushed
 * `[t0, t1]`; `tick` changes whenever the data may have changed (a refresh, a live poll), so
 * views reload on it. The view, run, range and theme live in the URL.
 */
export const useRunStore = defineStore("run", () => {
  const runs = ref([])
  const runId = ref(null)
  const info = ref(null)
  const programs = ref([])
  const view = ref("overview")
  const preset = ref(null)
  const brushed = ref(null)
  const theme = ref("light")
  const tick = ref(0)
  const error = ref(null)
  const selected = ref(null)
  // The machine part the Machine view's side panel shows: "dispatcher", "pool", "jobs",
  // "lane:<name>" or "unit:<name>" (null: none)
  const part = ref(null)
  // The Program view's graph view: "flowchart" (the default), "nodes" or "blocks"
  const graphView = ref("flowchart")
  // Whether a statement's time counts its wait for operands (time its inputs' statements spent)
  const withWait = ref(false)
  const phases = computed(() => (withWait.value ? ["wait", "queue", "service"] : ["queue", "service"]))
  let timer = null

  const live = computed(() => !!info.value && !info.value.stopped)
  const span = computed(() => info.value?.span || null)
  const range = computed(() => {
    const s = span.value
    if (!s) return null
    if (brushed.value) return brushed.value
    if (preset.value === null) return [s[0], s[1]]
    return [Math.max(s[0], s[1] - preset.value), s[1]]
  })
  const t0 = computed(() => range.value?.[0])
  const t1 = computed(() => range.value?.[1])
  const seconds = computed(() => (range.value ? Math.max(1, range.value[1] - range.value[0]) : 1))

  // Name tables: lane and unit ids in records -> names
  const names = computed(() => {
    const out = { lane: new Map(), unit: new Map() }
    for (const r of info.value?.names || []) out[r.kind]?.set(r.id, r)
    return out
  })
  const laneName = (id) => names.value.lane.get(id)?.name ?? `lane ${id}`
  const unitName = (id) => names.value.unit.get(id)?.name ?? `unit ${id}`
  const seriesNames = computed(() => Object.values(info.value?.series || {}).map((s) => s.name))

  async function loadRuns() {
    runs.value = await api.runs()
  }

  /** Show run `id`; switching from another run drops the selection and the brushed range. */
  async function open(id) {
    error.value = null
    if (runId.value !== null && runId.value !== id) {
      selected.value = null
      part.value = null
      unbrush()
    }
    runId.value = id
    try {
      const [i, p] = await Promise.all([api.info(id), api.programs(id)])
      info.value = i
      programs.value = p
      tick.value++
    } catch (e) {
      error.value = String(e.message || e)
    }
    schedule()
  }

  async function refresh() {
    if (!runId.value) return
    try {
      info.value = await api.info(runId.value)
      if (info.value && programs.value.length === 0) programs.value = await api.programs(runId.value)
      tick.value++
    } catch (e) {
      error.value = String(e.message || e)
    }
  }

  function schedule() {
    clearInterval(timer)
    timer = null
    if (live.value) timer = setInterval(refresh, LIVE_POLL_MS)
  }

  // The ranges a brush replaced, newest last: zooming out goes back through them
  const zooms = []

  function setPreset(seconds) {
    zooms.length = 0
    brushed.value = null
    preset.value = seconds
  }

  function brush(a, b) {
    if (!(b > a)) return
    zooms.push(brushed.value)
    brushed.value = [a, b]
  }

  /** Back one brush: the range it replaced (the preset after the first). */
  function zoomOut() {
    if (!brushed.value) return
    brushed.value = zooms.pop() ?? null
  }

  function unbrush() {
    zooms.length = 0
    brushed.value = null
  }

  function setTheme(t) {
    theme.value = t
    document.documentElement.classList.toggle("dark", t === "dark")
    try {
      localStorage.setItem("togeari-dash-theme", t)
    } catch {
      // A page without storage keeps the theme for this visit
    }
  }

  function fromUrl() {
    const q = new URLSearchParams(window.location.search)
    if (VIEWS.some(([v]) => v === q.get("view"))) view.value = q.get("view")
    const p = PRESETS.find(([label]) => label === q.get("range"))
    if (p) preset.value = p[1]
    if (q.get("t0") && q.get("t1")) brushed.value = [Number(q.get("t0")), Number(q.get("t1"))]
    if (q.get("node")) selected.value = q.get("node")
    withWait.value = q.get("wait") === "1"
    if (q.get("part")) part.value = q.get("part")
    if (["flowchart", "nodes", "blocks"].includes(q.get("graph"))) graphView.value = q.get("graph")
    let saved = null
    try {
      saved = localStorage.getItem("togeari-dash-theme")
    } catch {
      saved = null
    }
    setTheme(q.get("theme") || saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"))
    return q.get("run")
  }

  function toUrl() {
    const q = new URLSearchParams()
    if (runId.value) q.set("run", runId.value)
    q.set("view", view.value)
    if (brushed.value) {
      q.set("t0", brushed.value[0].toFixed(0))
      q.set("t1", brushed.value[1].toFixed(0))
    } else q.set("range", PRESETS.find(([, s]) => s === preset.value)?.[0] || "all")
    if (selected.value) q.set("node", selected.value)
    if (withWait.value) q.set("wait", "1")
    if (part.value) q.set("part", part.value)
    if (graphView.value !== "flowchart") q.set("graph", graphView.value)
    history.replaceState(null, "", `?${q}`)
  }

  watch([runId, view, preset, brushed, selected, withWait, part, graphView], toUrl)
  watch(live, schedule)

  return {
    runs,
    runId,
    info,
    programs,
    view,
    preset,
    brushed,
    theme,
    tick,
    error,
    selected,
    withWait,
    phases,
    part,
    graphView,
    live,
    span,
    range,
    t0,
    t1,
    seconds,
    names,
    laneName,
    unitName,
    seriesNames,
    loadRuns,
    open,
    refresh,
    setPreset,
    brush,
    zoomOut,
    unbrush,
    setTheme,
    fromUrl,
  }
})
