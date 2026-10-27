import { useRunStore } from "@/stores/run"

/**
 * What is picked in the open run: `node`, the Program view's statement (its path on the shown
 * graph), and `part`, the Machine view's part ("dispatcher", "pool", "jobs", "lane:<name>" or
 * "unit:<name>"). Opening another run clears both.
 */
export const useSelectionStore = defineStore("selection", () => {
  const run = useRunStore()
  const node = ref(null)
  const part = ref(null)

  watch(
    () => run.runId,
    () => {
      node.value = null
      part.value = null
    },
  )

  return { node, part }
})
