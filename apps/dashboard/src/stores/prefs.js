import { readPref, writePref } from "@/utils/prefs"

export const GRAPH_VIEWS = ["flowchart", "nodes", "blocks"]

/** Rate charts' resolution: [value, label]; "auto" one bucket per screen pixel, "full" every
 * stored row, a number a fixed step in seconds. */
export const RESOLUTIONS = [
  ["auto", "Auto"],
  ["full", "Full"],
  [10, "10 s"],
  [60, "1 min"],
  [300, "5 min"],
  [900, "15 min"],
  [3600, "1 h"],
]

/**
 * How the views show things, saved across visits: `withWait` (statement time counts its wait
 * for operands), `graphView` (the Program view's KIR view) and `resolution` (rate charts'
 * buckets). `phases` follows `withWait`.
 */
export const usePrefsStore = defineStore("prefs", () => {
  const withWait = ref(readPref("withWait", false) === true)
  const saved = readPref("graphView", "flowchart")
  const graphView = ref(GRAPH_VIEWS.includes(saved) ? saved : "flowchart")
  const savedRes = readPref("resolution", "auto")
  const resolution = ref(RESOLUTIONS.some(([r]) => r === savedRes) ? savedRes : "auto")
  const phases = computed(() =>
    withWait.value ? ["wait", "queue", "service"] : ["queue", "service"],
  )

  watch(withWait, (v) => writePref("withWait", v))
  watch(graphView, (v) => writePref("graphView", v))
  watch(resolution, (v) => writePref("resolution", v))

  return { withWait, graphView, resolution, phases }
})
