import { readPref, writePref } from "@/utils/prefs"

export const GRAPH_VIEWS = ["flowchart", "nodes", "blocks"]

/**
 * How the views show things, saved across visits: `withWait` (statement time counts its wait
 * for operands) and `graphView` (the Program view's KIR view). `phases` follows `withWait`.
 */
export const usePrefsStore = defineStore("prefs", () => {
  const withWait = ref(readPref("withWait", false) === true)
  const saved = readPref("graphView", "flowchart")
  const graphView = ref(GRAPH_VIEWS.includes(saved) ? saved : "flowchart")
  const phases = computed(() =>
    withWait.value ? ["wait", "queue", "service"] : ["queue", "service"],
  )

  watch(withWait, (v) => writePref("withWait", v))
  watch(graphView, (v) => writePref("graphView", v))

  return { withWait, graphView, phases }
})
