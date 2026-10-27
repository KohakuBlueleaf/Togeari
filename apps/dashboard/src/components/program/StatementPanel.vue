<template>
  <div class="flex flex-col">
    <div class="px-5 pt-4 pb-3 kt-v2-line border-b">
      <div class="flex items-center gap-2">
        <span class="text-[15px] font-semibold">{{ group.node }}</span>
        <span v-if="row?.op" class="chip-warm">{{ row.op }}</span>
        <button class="btn-icon ml-auto" title="Close" @click="selection.node = null"><span class="i-carbon-close" /></button>
      </div>
      <div class="mt-0.5 num text-[11px] text-warm-500">
        {{ group.path }}<template v-if="row?.line"> · line {{ row.line }}</template>
      </div>
      <div v-if="rows.length > 1" class="mt-2 flex items-center gap-1.5 flex-wrap">
        <span class="text-[11px] text-warm-500">Address</span>
        <button v-for="r in rows" :key="r.addr" :class="r.addr === addr ? 'chip-iolite' : 'chip-warm'" @click="addr = r.addr">@{{ r.addr }}</button>
      </div>
    </div>

    <div class="grid grid-cols-3 gap-px kt-v2-line border-b bg-[var(--v2-line)]">
      <div v-for="t in tiles" :key="t.label" class="bg-[var(--v2-panel)] px-4 py-2.5">
        <div class="section-title">{{ t.label }}</div>
        <div class="num text-[15px] font-semibold mt-0.5" :class="t.tone">{{ t.value }}</div>
      </div>
    </div>

    <div class="px-5 py-4 kt-v2-line border-b">
      <div class="section-title mb-2">Phases per call</div>
      <table class="w-full text-xs">
        <thead class="text-warm-400">
          <tr>
            <th class="text-left font-medium pb-1">Phase</th>
            <th class="text-right font-medium pb-1">mean</th>
            <th class="text-right font-medium pb-1">p50</th>
            <th class="text-right font-medium pb-1">p90</th>
            <th class="text-right font-medium pb-1">p99</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in PHASES" :key="p" :title="PHASE_NOTE[p]">
            <td class="py-1"><span class="inline-block w-2 h-2 rounded-[2px] mr-1.5" :style="{ background: phase(p) }" />{{ PHASE_LABEL[p] }}</td>
            <td class="text-right num">{{ us(row?.[p]?.mean) }}</td>
            <td class="text-right num">{{ us(row?.[p]?.p50) }}</td>
            <td class="text-right num">{{ us(row?.[p]?.p90) }}</td>
            <td class="text-right num">{{ us(row?.[p]?.p99) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="px-5 py-4 kt-v2-line border-b">
      <div class="flex items-center mb-2">
        <span class="section-title">{{ PHASE_LABEL[metric] }} p90 over time</span>
        <Segmented v-model="metric" :options="METRIC_OPTIONS" class="ml-auto" />
      </div>
      <TimeChart :x="latency.x" :series="latency.series" :format="us" :height="150" />
      <div class="mt-1 text-[10px] text-warm-400">Band: least and greatest value in each bucket.</div>
    </div>

    <div class="px-5 py-4 kt-v2-line border-b">
      <div class="section-title mb-2">Calls per second</div>
      <TimeChart :x="calls.x" :series="calls.series" :format="(v) => rate(v)" :height="110" />
    </div>

    <div class="px-5 py-4">
      <div class="section-title mb-2">{{ PHASE_LABEL[metric] }} distribution</div>
      <HistChart :hist="hist" :phase="metric" />
      <div v-if="hist?.n" class="mt-1.5 flex gap-3 text-[11px] text-warm-500 num">
        <span>n {{ count(hist.n) }}</span
        ><span>min {{ us(hist.min) }}</span
        ><span>max {{ us(hist.max) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue"

import HistChart from "@/components/charts/HistChart.vue"
import TimeChart from "@/components/charts/TimeChart.vue"
import Segmented from "@/components/ui/Segmented.vue"
import { useLoad } from "@/composables/useLoad"
import { useStep } from "@/composables/useStep"
import { useSelectionStore } from "@/stores/selection"
import { useThemeStore } from "@/stores/theme"
import { api } from "@/utils/api"
import { bucket } from "@/utils/fine"
import { count, pct, rate, us } from "@/utils/format"
import { PHASE_LABEL, PHASE_NOTE, PHASES, phase, series } from "@/utils/palette"

/** One graph element's statements: totals, phase quantiles, latency and calls over time, the distribution. */
const props = defineProps({
  group: { type: Object, required: true },
  rows: { type: Array, default: () => [] },
})

const METRIC_OPTIONS = PHASES.map((p) => [p, PHASE_LABEL[p]])
const selection = useSelectionStore()
const theme = useThemeStore()
const addr = ref(null)
const metric = ref("service")

watch(
  () => props.rows.map((r) => r.addr).join(","),
  () => {
    if (!props.rows.some((r) => r.addr === addr.value)) addr.value = props.rows[0]?.addr ?? null
    const r = props.rows[0]
    if (r && !r.service && r.queue) metric.value = "queue"
  },
  { immediate: true },
)
const row = computed(() => props.rows.find((r) => r.addr === addr.value) || null)
const entity = computed(() => (addr.value === null ? null : `node:${addr.value}`))

const tiles = computed(() => {
  const g = props.group
  return [
    { label: "Calls", value: count(g.calls) },
    { label: "Per second", value: rate(g.rate) },
    {
      label: "Failed",
      value: g.errors ? `${count(g.errors)} · ${pct(g.errors / Math.max(1, g.calls), 1)}` : "—",
      tone: g.errors ? "text-coral-shadow dark:text-coral-light" : "",
    },
  ]
})

// Calls per second from every stored row, bucketed here; the p90 needs histograms, merged by
// the server at the same step
const { step, clip } = useStep()
const { data: lat } = useLoad(
  async (r, { signal, previous, refresh }) => {
    if (!entity.value) return null
    const opts = { t0: r.t0, t1: r.t1, step: step.value, stat: "p90", signal, previous: previous && [previous], refresh }
    return (await api.seriesEach(r.runId, [entity.value], metric.value, opts))[0]
  },
  () => [entity.value, metric.value, step.value],
)
const { data: callRows } = useLoad(
  (r, { signal, previous, refresh }) => (entity.value ? api.fine(r.runId, [[entity.value, "calls"]], { t0: r.t0, t1: r.t1, signal, previous, refresh }) : null),
  () => entity.value,
)
const callSeries = computed(() => (callRows.value?.series?.[0] ? bucket(callRows.value.series[0], { stat: "rate", step: step.value, clip: clip.value }) : null))
const { data: hist } = useLoad(
  (r) => (entity.value ? api.hist(r.runId, entity.value, metric.value, r.t0, r.t1) : null),
  () => [entity.value, metric.value],
)

const latency = computed(() => {
  const s = lat.value
  void theme.dark
  if (!s?.t?.length) return { x: [], series: [] }
  return { x: s.t, series: [{ label: "p90", values: s.v, color: phase(metric.value), band: [s.lo, s.hi] }] }
})
const calls = computed(() => {
  const s = callSeries.value
  void theme.dark
  if (!s?.t?.length) return { x: [], series: [] }
  return { x: s.t, series: [{ label: "calls/s", values: s.v, color: series(0), fill: true }] }
})
</script>
