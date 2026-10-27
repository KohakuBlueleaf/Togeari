import { api } from "@/utils/api"

const LIVE_POLL_MS = 2000

/**
 * The open run: its record (`info`: machine.json, time span, name tables, series names, the
 * machine) and its programs. The route opens it (`/run/<id>/...`). `tick` changes whenever its
 * data may have changed (opened, refreshed, a live poll), so views reload on it; while the run
 * is live its record is read again every `LIVE_POLL_MS`.
 */
export const useRunStore = defineStore("run", () => {
  const runId = ref(null)
  const info = ref(null)
  const programs = ref([])
  const tick = ref(0)
  const error = ref(null)
  let timer = null

  const live = computed(() => !!info.value && !info.value.stopped)
  const span = computed(() => info.value?.span || null)

  // Name tables: lane and unit ids in records -> names
  const names = computed(() => {
    const out = { lane: new Map(), unit: new Map() }
    for (const r of info.value?.names || []) out[r.kind]?.set(r.id, r)
    return out
  })
  const laneName = (id) => names.value.lane.get(id)?.name ?? `lane ${id}`
  const unitName = (id) => names.value.unit.get(id)?.name ?? `unit ${id}`
  const seriesNames = computed(() => Object.values(info.value?.series || {}).map((s) => s.name))

  /** Show run `id` (again: a no-op once loaded). */
  async function open(id) {
    if (id === runId.value && (info.value || error.value)) return
    runId.value = id
    info.value = null
    programs.value = []
    error.value = null
    try {
      const [i, p] = await Promise.all([api.info(id), api.programs(id)])
      if (runId.value !== id) return
      info.value = i
      programs.value = p
      tick.value++
    } catch (e) {
      if (runId.value === id) error.value = String(e.message || e)
    }
    schedule()
  }

  function close() {
    runId.value = null
    info.value = null
    programs.value = []
    error.value = null
    schedule()
  }

  async function refresh() {
    const id = runId.value
    if (!id) return
    try {
      const i = await api.info(id)
      if (runId.value !== id) return
      info.value = i
      if (programs.value.length === 0) programs.value = await api.programs(id)
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

  watch(live, schedule)

  return {
    runId,
    info,
    programs,
    tick,
    error,
    live,
    span,
    names,
    laneName,
    unitName,
    seriesNames,
    open,
    close,
    refresh,
  }
})
