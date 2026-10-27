import { defineStore } from "pinia"
import { computed, ref, watch } from "vue"

import { api } from "@/utils/api"
import { useRunStore } from "@/stores/run"

/** The machine's rare events, kept from the stream while the run is live, with their tone. */
export const RARE = {
  "unit.up": "good",
  "unit.down": "bad",
  "unit.attach": "idle",
  "unit.detach": "idle",
  "unit.detached": "idle",
  "lane.dead": "bad",
  "plugin.disabled": "warn",
  "snapshot.error": "warn",
  "fault.taken": "warn",
  "machine.start": "idle",
  "machine.stop": "idle",
  "machine.error": "bad",
}

// The queries read every poll, and how often: once a second while live and the page is shown
const POLLED = ["stats", "lanes", "knob_specs", "jobs", "plugins"]
const POLL_MS = 1000
// A run that claims to be live and does not answer is probed again this often
const PROBE_MS = 5000
const KEEP = 200

/**
 * The run's machine while it is live, through the dash server's proxy to its inspector.
 *
 * `state` is the server's probe: "live", "unreachable" (claims live, no inspector answers) or
 * "stopped". While live: `now` holds the answers of the `POLLED` queries (refreshed each second,
 * not while the page is hidden) and `events` the last `KEEP` rare events, newest first.
 * `command(name, body)` sends a command and polls again; its answer or error is the caller's.
 */
export const useLiveStore = defineStore("live", () => {
  const run = useRunStore()
  const state = ref(null)
  const commands = ref([])
  const now = ref({})
  const at = ref(null)
  const events = ref([])
  const error = ref(null)
  const live = computed(() => state.value === "live")
  let timer = null
  let source = null
  let polling = false
  let gen = 0

  function stop() {
    clearTimeout(timer)
    timer = null
    source?.close()
    source = null
  }

  /** Probe the run; while live, poll and stream, else probe again if it claims to be live. */
  async function probe() {
    const mine = gen
    const id = run.runId
    let got
    try {
      got = await api.state(id)
    } catch (e) {
      got = { state: "unreachable", error: String(e.message || e) }
    }
    if (mine !== gen) return
    state.value = got.state
    commands.value = got.commands || []
    if (got.state === "live") {
      stream(id)
      await poll()
    } else if (got.state === "unreachable") {
      stop()
      timer = setTimeout(probe, PROBE_MS)
    } else stop()
  }

  async function poll() {
    const mine = gen
    clearTimeout(timer)
    if (polling || !live.value) return
    if (!document.hidden) {
      polling = true
      const got = await Promise.allSettled(POLLED.map((q) => api.live.query(run.runId, q)))
      polling = false
      if (mine !== gen) return
      const failed = got.filter((g) => g.status === "rejected")
      if (failed.length === got.length) {
        // The inspector went away: the run stopped or was killed
        error.value = String(failed[0].reason?.message || failed[0].reason)
        stop()
        await probe()
        return
      }
      const next = { ...now.value }
      got.forEach((g, i) => {
        if (g.status === "fulfilled") next[POLLED[i]] = g.value
      })
      now.value = next
      at.value = Date.now() / 1000
      error.value = failed.length ? String(failed[0].reason?.message || failed[0].reason) : null
    }
    if (live.value) timer = setTimeout(poll, POLL_MS)
  }

  function stream(id) {
    if (source) return
    source = api.live.events(id, Object.keys(RARE))
    for (const kind of Object.keys(RARE)) {
      source.addEventListener(kind, (msg) => {
        let ev
        try {
          ev = JSON.parse(msg.data)
        } catch {
          return
        }
        events.value = [ev, ...events.value].slice(0, KEEP)
        if (kind.startsWith("unit.") || kind === "lane.dead") poll()
      })
    }
  }

  async function command(name, body = {}) {
    const answer = await api.live.command(run.runId, name, body)
    await poll()
    return answer
  }

  function reset() {
    gen++
    stop()
    polling = false
    state.value = null
    commands.value = []
    now.value = {}
    at.value = null
    events.value = []
    error.value = null
  }

  watch(
    () => run.runId,
    (id) => {
      reset()
      if (id) probe()
    },
    { immediate: true },
  )
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && live.value) poll()
  })

  return { state, commands, now, at, events, error, live, command, probe }
})
