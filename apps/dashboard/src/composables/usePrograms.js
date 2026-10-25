import { ref, watch } from "vue"

import { api } from "@/api/client"
import { useRunStore } from "@/stores/run"
import { statementIndex } from "@/utils/statements"

/** Every program of the run with its statements and graphs (loaded once per run and program). */
export function usePrograms() {
  const run = useRunStore()
  const programs = ref([])
  const index = ref(new Map())
  const cache = new Map()

  watch(
    () => [run.runId, run.programs.map((p) => `${p.start}:${(p.entries || []).length}`).join(",")],
    async ([id]) => {
      if (!id) return
      const full = await Promise.all(
        run.programs.map(async (p) => {
          const key = `${id}/${p.start}/${(p.entries || []).length}`
          if (!cache.has(key)) cache.set(key, api.program(id, p.start))
          return cache.get(key)
        }),
      )
      programs.value = full.filter(Boolean)
      index.value = statementIndex(programs.value)
    },
    { immediate: true },
  )
  return { programs, index }
}
