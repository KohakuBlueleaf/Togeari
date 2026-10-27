<template>
  <div class="h-full flex min-h-0" :class="compact ? 'flex-col' : ''">
    <!-- Kept traces -->
    <SidePane name="jobs" side="left" label="Kept traces" icon="i-carbon-list-boxes" :initial="400" :min="330" :scroll="false">
      <div class="h-11 px-4 flex items-center gap-2 kt-v2-line border-b shrink-0">
        <template v-if="!compact">
          <span class="i-carbon-list-boxes text-warm-500" />
          <span class="card-title">Kept traces</span>
        </template>
        <span class="num text-[11px] text-warm-400">{{ traces?.length ?? 0 }}<template v-if="compact"> kept</template></span>
        <Segmented v-model="reason" :options="REASONS" class="ml-auto" />
      </div>
      <div class="flex-1 overflow-auto">
        <table class="w-full text-xs">
          <thead class="text-warm-400 sticky top-0 bg-[var(--v2-panel)]">
            <tr class="kt-v2-line border-b">
              <th class="text-left font-medium px-4 h-8">Ended</th>
              <th class="text-right font-medium px-2">Job</th>
              <th class="text-left font-medium px-2">Kept</th>
              <th class="text-right font-medium px-4">Wall</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in traces || []" :key="t.key" class="kt-v2-line border-b cursor-pointer kt-row" :class="t.key === key ? 'kt-row--on' : ''" @click="key = t.key">
              <td class="px-4 h-9 num text-warm-600 dark:text-warm-300">{{ clock(t.t_end) }}</td>
              <td class="px-2 text-right num">{{ t.job }}</td>
              <td class="px-2">
                <span :class="REASON_CHIP[t.reason]"> <span v-if="t.reason === 'failed'" class="i-carbon-error-filled text-[10px]" />{{ t.reason }} </span>
              </td>
              <td class="px-4 text-right num">{{ us(t.wall_us) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="!traces?.length" class="px-4 py-4 text-xs text-warm-400">No traces kept in this range.</p>
      </div>
    </SidePane>

    <div class="flex-1 min-w-0 overflow-auto flex flex-col gap-4" :class="compact ? 'p-3' : 'p-5'">
      <!-- Resident jobs of the live machine, as one status card -->
      <SectionCard v-if="live.live" title="Resident jobs" icon="i-carbon-layers" :count="resident.n" hint="live" flush>
        <template #actions
          ><span class="chip-sage"><span class="i-carbon-dot-mark text-[10px] animate-pulse" />live</span></template
        >
        <div class="grid gap-x-6 gap-y-2 px-4 py-3" :class="compact ? 'grid-cols-2' : 'grid-cols-3 lg:grid-cols-6'">
          <div v-for="s in residentStats" :key="s.label" class="min-w-0" :title="s.title">
            <div class="section-title">{{ s.label }}</div>
            <div class="num text-[18px] leading-6 font-semibold text-warm-800 dark:text-warm-100">{{ s.value }}</div>
          </div>
        </div>
      </SectionCard>

      <!-- The trace -->
      <SectionCard v-if="trace" :title="`Job ${trace.job}`" icon="i-carbon-chart-waterfall" :hint="`${us(fall.total)} · ${shown.rows.length} of ${fall.rows.length} statement runs`" flush>
        <template #actions>
          <span :class="REASON_CHIP[trace.reason]">{{ trace.reason }}</span>
          <ChartLegend :items="phaseLegend" />
        </template>
        <div class="px-4 pt-3 flex flex-wrap items-center gap-1.5 text-[11px]">
          <span class="text-warm-400 mr-1">Show</span>
          <button v-for="c in categories" :key="c.name" class="kt-cat" :class="hidden.includes(c.name) ? 'kt-cat--off' : ''" :aria-pressed="!hidden.includes(c.name)" :title="hidden.includes(c.name) ? `Show ${c.name} calls` : `Hide ${c.name} calls`" @click="toggle(c.name)">
            {{ c.name }} <span class="num opacity-60">{{ c.n }}</span>
          </button>
        </div>
        <div class="px-4 py-3">
          <Waterfall :fall="shown" />
          <p v-if="!shown.rows.length" class="text-xs text-warm-400">Every category is hidden.</p>
        </div>
      </SectionCard>
      <div v-else class="flex-1 flex items-center justify-center text-xs text-warm-400">Pick a trace.</div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue"

import ChartLegend from "@/components/charts/ChartLegend.vue"
import Waterfall from "@/components/jobs/Waterfall.vue"
import SidePane from "@/components/shell/SidePane.vue"
import SectionCard from "@/components/ui/SectionCard.vue"
import Segmented from "@/components/ui/Segmented.vue"
import { useCompact } from "@/composables/useCompact"
import { useLoad } from "@/composables/useLoad"
import { usePrograms } from "@/composables/usePrograms"
import { useLiveStore } from "@/stores/live"
import { useRunStore } from "@/stores/run"
import { useThemeStore } from "@/stores/theme"
import { api } from "@/utils/api"
import { clock, count, us } from "@/utils/format"
import { PHASE_LABEL, PHASES, phase } from "@/utils/palette"
import { waterfall } from "@/utils/waterfall"

const REASONS = [
  ["", "All"],
  ["failed", "Failed"],
  ["slowest", "Slowest"],
  ["sampled", "Sampled"],
]
const REASON_CHIP = { failed: "chip-coral", slowest: "chip-amber", sampled: "chip-warm" }

const run = useRunStore()
const theme = useThemeStore()
const compact = useCompact()
const { index } = usePrograms()
const reason = ref("")
const key = ref(null)

const { data: traces } = useLoad(
  (r) => api.traces(r.runId, r.t0, r.t1 + 1, reason.value || null, 300),
  () => reason.value,
  [],
)
watch(traces, (ts) => {
  if (!ts?.some((t) => t.key === key.value)) key.value = ts?.[0]?.key ?? null
})
const { data: trace } = useLoad(
  (r) => (key.value ? api.trace(r.runId, key.value) : null),
  () => key.value,
)

const fall = computed(() => waterfall(trace.value, index.value, run.laneName, run.unitName))

// Call categories (the row's lane, or inline / store hit) the viewer hides; kept per browser
const HIDDEN_KEY = "togeari.jobs.hidden"
const hidden = ref(load())
function load() {
  try {
    return JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]")
  } catch {
    return []
  }
}
function toggle(name) {
  hidden.value = hidden.value.includes(name) ? hidden.value.filter((n) => n !== name) : [...hidden.value, name]
  try {
    localStorage.setItem(HIDDEN_KEY, JSON.stringify(hidden.value))
  } catch {
    // storage refused: the choice lasts for this page only
  }
}
const categories = computed(() => {
  const n = new Map()
  for (const r of fall.value.rows) n.set(r.lane || "other", (n.get(r.lane || "other") || 0) + 1)
  return [...n].map(([name, k]) => ({ name, n: k })).sort((a, b) => b.n - a.n)
})
const shown = computed(() => ({ ...fall.value, rows: fall.value.rows.filter((r) => !hidden.value.includes(r.lane || "other")) }))

// The live machine's resident jobs, summed
const live = useLiveStore()
const resident = computed(() => {
  const jobs = live.now.jobs || []
  const sum = (k) => jobs.reduce((a, j) => a + (j[k] || 0), 0)
  const ids = jobs.map((j) => j.id)
  return {
    n: jobs.length,
    paused: jobs.filter((j) => j.paused).length,
    strands: sum("strands"),
    window: sum("window"),
    unfinished: sum("unfinished"),
    oldest: ids.length ? Math.min(...ids) : null,
  }
})
const residentStats = computed(() => {
  const r = resident.value
  return [
    { label: "Resident", value: count(r.n) },
    { label: "Running", value: count(r.n - r.paused) },
    { label: "Paused", value: count(r.paused) },
    { label: "Strands", value: count(r.strands) },
    { label: "Window", value: count(r.window), title: "statements fetched and not yet retired, all resident jobs" },
    { label: "Oldest job", value: r.oldest ?? "—", title: `${count(r.unfinished)} statements unfinished in all` },
  ]
})
const phaseLegend = computed(() => (void theme.dark, PHASES.map((p) => ({ label: PHASE_LABEL[p], color: phase(p) }))))
</script>
