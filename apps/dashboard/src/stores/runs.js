import { api } from "@/utils/api"

/** The runs under the dashboard's roots, newest first, each with its probed state. */
export const useRunsStore = defineStore("runs", () => {
  const list = ref([])
  const loaded = ref(false)
  const error = ref(null)

  async function load() {
    try {
      list.value = await api.runs()
      error.value = null
    } catch (e) {
      error.value = String(e.message || e)
    } finally {
      loaded.value = true
    }
  }

  /** A run by its id or its name. */
  const find = (key) =>
    list.value.find((r) => r.id === key) || list.value.find((r) => r.name === key) || null

  return { list, loaded, error, load, find }
})
