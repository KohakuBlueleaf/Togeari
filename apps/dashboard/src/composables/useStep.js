import { computed, onBeforeUnmount, ref } from "vue"

import { usePrefsStore } from "@/stores/prefs"
import { useRangeStore } from "@/stores/range"
import { useRunStore } from "@/stores/run"
import { stepFor } from "@/utils/fine"

/**
 * The bucket step (seconds) rate charts use, from the saved resolution: "full" every stored
 * row (1, the finest), "auto" one bucket per pixel of the window's width over the range, a
 * number that step. `clip`: the seconds buckets count, the range from the run's start.
 */
export function useStep() {
  const prefs = usePrefsStore()
  const range = useRangeStore()
  const run = useRunStore()
  const width = ref(window.innerWidth)
  const resize = () => (width.value = window.innerWidth)
  window.addEventListener("resize", resize)
  onBeforeUnmount(() => window.removeEventListener("resize", resize))

  const step = computed(() => {
    const r = prefs.resolution
    if (r === "full") return 1
    if (r === "auto") return stepFor(range.seconds, width.value)
    return r
  })
  const clip = computed(() => [
    Math.max(range.t0 ?? -Infinity, run.info?.started ?? -Infinity),
    range.t1 ?? Infinity,
  ])
  return { step, clip }
}
