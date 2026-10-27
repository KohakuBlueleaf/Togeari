import { useRunStore } from "@/stores/run"
import { readPref, writePref } from "@/utils/prefs"

/** Range presets: seconds back from the run's last bucket; "all" is the whole run. */
export const PRESETS = [
  ["1m", 60],
  ["5m", 300],
  ["15m", 900],
  ["1h", 3600],
  ["6h", 21600],
  ["all", null],
]

/**
 * The time range every view reads: a preset (saved; seconds back from the end of the run, null
 * for all of it) or a brushed `[t0, t1]`. Brushes stack: zooming out goes back through them.
 * Opening another run drops the brushes.
 */
export const useRangeStore = defineStore("range", () => {
  const run = useRunStore()
  const saved = readPref("preset", null)
  const preset = ref(PRESETS.some(([, s]) => s === saved) ? saved : null)
  const brushed = ref(null)
  // The ranges a brush replaced, newest last
  const zooms = []

  const range = computed(() => {
    const s = run.span
    if (!s) return null
    if (brushed.value) return brushed.value
    if (preset.value === null) return [s[0], s[1]]
    return [Math.max(s[0], s[1] - preset.value), s[1]]
  })
  const t0 = computed(() => range.value?.[0])
  const t1 = computed(() => range.value?.[1])
  const seconds = computed(() => (range.value ? Math.max(1, range.value[1] - range.value[0]) : 1))

  function setPreset(seconds) {
    zooms.length = 0
    brushed.value = null
    preset.value = seconds
    writePref("preset", seconds)
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

  watch(() => run.runId, unbrush)

  return { preset, brushed, range, t0, t1, seconds, setPreset, brush, zoomOut, unbrush }
})
