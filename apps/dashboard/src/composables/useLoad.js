import { ref, shallowRef, watch } from "vue"

import { useRangeStore } from "@/stores/range"
import { useRunStore } from "@/stores/run"

/**
 * `fn(q, ctx)` loaded again whenever the run, its range or its tick change (and on any extra
 * `deps`). `q` is `{ runId, t0, t1, info }`; `ctx` is `{ signal, emit, previous, refresh }`:
 * `signal` aborts when a newer load replaces this one, `emit(value)` shows a partial result
 * before `fn` returns, `previous` is the value on screen for the same run (else `initial`),
 * `refresh` says it came from a load of the same run, chosen range and `deps`.
 *
 * A change of the run, the chosen range (preset or brush) or `deps` aborts the load in flight
 * and starts again. A live refresh (only the tick and the range's ends moved) while a load is in
 * flight waits for it and then loads once, so slow loads never pile up.
 *
 * `data` is a shallow ref: a loaded value is replaced whole, never made deeply reactive (its
 * arrays are read without proxies). Partial values reach the screen at most once a frame.
 */
export function useLoad(fn, deps = () => null, initial = null) {
  const run = useRunStore()
  const range = useRangeStore()
  const data = shallowRef(initial)
  const loading = ref(false)
  const error = ref(null)
  let seq = 0
  let shown = null
  let shownKey = null
  let key = null
  let flight = null
  let again = false

  async function load() {
    if (!run.runId || !range.range) return
    flight?.abort()
    const mine = ++seq
    const ctl = new AbortController()
    flight = ctl
    loading.value = true
    const current = () => mine === seq
    let partial = null
    let frame = null
    const emit = (value) => {
      partial = value
      frame ??= requestAnimationFrame(() => {
        frame = null
        if (current() && partial !== null) data.value = partial
      })
    }
    const previous = shown === run.runId ? data.value : initial
    const refresh = shownKey === key
    const mineKey = key
    const q = { runId: run.runId, t0: range.t0, t1: range.t1, info: run.info }
    try {
      const got = await fn(q, { signal: ctl.signal, emit, previous, refresh })
      partial = null
      if (current()) {
        data.value = got
        shown = q.runId
        shownKey = mineKey
        error.value = null
      }
    } catch (e) {
      if (current() && e?.name !== "AbortError") error.value = String(e.message || e)
    } finally {
      if (current()) {
        loading.value = false
        flight = null
        if (again) {
          again = false
          load()
        }
      }
    }
  }

  function changed() {
    const next = JSON.stringify([run.runId, range.preset, range.brushed, deps()])
    if (next !== key || !flight) {
      key = next
      again = false
      load()
    } else again = true
  }

  watch(
    () => [run.runId, range.t0, range.t1, run.tick, range.preset, range.brushed, deps()],
    changed,
    { immediate: true, deep: true },
  )
  return { data, loading, error, load }
}
