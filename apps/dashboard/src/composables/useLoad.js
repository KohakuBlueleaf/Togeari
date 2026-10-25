import { ref, watch } from "vue"

import { useRunStore } from "@/stores/run"

/**
 * `fn(run)` loaded again whenever the run, its range or its tick change (and on any extra
 * `deps`); a load that finishes after a newer one started is dropped. `run` is the store.
 */
export function useLoad(fn, deps = () => null, initial = null) {
  const run = useRunStore()
  const data = ref(initial)
  const loading = ref(false)
  const error = ref(null)
  let seq = 0

  async function load() {
    if (!run.runId || !run.range) return
    const mine = ++seq
    loading.value = true
    try {
      const got = await fn(run)
      if (mine === seq) {
        data.value = got
        error.value = null
      }
    } catch (e) {
      if (mine === seq) error.value = String(e.message || e)
    } finally {
      if (mine === seq) loading.value = false
    }
  }

  watch(() => [run.runId, run.t0, run.t1, run.tick, deps()], load, { immediate: true, deep: true })
  return { data, loading, error, load }
}
