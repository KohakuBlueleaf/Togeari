<template>
  <div class="p-3 md:p-5 flex flex-col gap-4 max-w-[1600px] mx-auto">
    <!-- Headline numbers -->
    <div class="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-3">
      <StatTile label="Jobs done" icon="i-carbon-checkmark-outline" :value="count(jobs.done)" :note="`${rate(jobs.done / run.seconds)} / s`" :trend="throughputTrend" />
      <StatTile label="Job time p50" icon="i-carbon-time" :value="us(jobs.p50)" :note="`p90 ${us(jobs.p90)} · p99 ${us(jobs.p99)}`" />
      <StatTile
        label="Failed jobs"
        icon="i-carbon-warning-alt"
        :value="count(jobs.failed)"
        :status="jobs.failed ? 'bad' : 'good'"
        :status-text="jobs.failed ? pct(jobs.failed / Math.max(1, jobs.admitted)) : 'none'"
        :note="jobs.failed ? 'failed or cancelled' : null"
      />
      <StatTile label="Calls" icon="i-carbon-function" :value="rate(calls.rate)" unit="/ s" :note="`${count(calls.n)} in range`" />
      <StatTile
        label="Failed attempts"
        icon="i-carbon-error"
        :value="count(calls.errors)"
        :status="calls.errors ? 'warn' : 'good'"
        :status-text="calls.errors ? pct(calls.errors / Math.max(1, calls.n), 2) : 'none'"
        :note="calls.faults ? `${count(calls.faults)} reached a job` : null"
      />
      <StatTile label="Resident jobs" icon="i-carbon-layers" :value="count(resident.last)" :note="`peak ${count(resident.peak)}`" :trend="resident.values" />
    </div>

    <!-- Throughput and where the time goes -->
    <div class="grid grid-cols-1 xl:grid-cols-5 gap-4">
      <SectionCard class="xl:col-span-3" title="Throughput" icon="i-carbon-chart-line" hint="jobs per second, by bucket">
        <template #actions><ChartLegend :items="throughputLegend" /></template>
        <TimeChart :x="throughput.x" :series="throughput.series" :format="rateFmt" :height="220" />
      </SectionCard>
      <SectionCard class="xl:col-span-2" title="Where the time goes" icon="i-carbon-chart-bar" :count="timeRows.length" hint="mean per call, by total time" flush>
        <template #actions>
          <ChartLegend :items="phaseLegend" />
          <WaitToggle />
        </template>
        <div class="px-4 py-3 flex flex-col gap-2.5">
          <button
            v-for="r in timeRows"
            :key="r.addr"
            class="text-left grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)_4.5rem] items-center gap-3 rounded-md -mx-1.5 px-1.5 py-0.5 kt-row"
            @click="openStatement(r)"
          >
            <span class="truncate text-xs"><span class="font-medium">{{ r.node }}</span><span class="text-warm-400 num text-[10px] ml-1">@{{ r.addr }}</span></span>
            <PhaseBar :wait="run.withWait ? mean(r, 'wait') : 0" :queue="mean(r, 'queue')" :service="mean(r, 'service')" :frac="r.perCall / maxPerCall" />
            <span class="num text-[11px] text-right text-warm-600 dark:text-warm-300">{{ pct(r.share, 0) }} <span class="text-warm-400">time</span></span>
          </button>
          <p v-if="!timeRows.length" class="text-xs text-warm-400">No calls in this range.</p>
        </div>
      </SectionCard>
    </div>

    <!-- The live machine's rare events -->
    <SectionCard v-if="live.live" title="Machine events" icon="i-carbon-activity" :count="live.events.length" hint="units, lanes, plugins, the machine · newest first" flush>
      <template #actions><span class="chip-sage"><span class="i-carbon-dot-mark text-[10px] animate-pulse" />live</span></template>
      <EventFeed :events="live.events" :limit="8" />
    </SectionCard>

    <!-- Lanes -->
    <SectionCard title="Lanes" icon="i-carbon-flow-stream" :count="lanes.length" flush>
      <table class="w-full text-xs">
        <thead class="text-warm-400">
          <tr class="kt-v2-line border-b">
            <th class="text-left font-medium px-4 h-8">Lane</th>
            <th class="text-right font-medium px-3">Units</th>
            <th class="text-right font-medium px-3">Items / s</th>
            <th class="text-right font-medium px-3">Batch</th>
            <th class="text-right font-medium px-3">Queue p50</th>
            <th class="text-right font-medium px-3">Queue p90</th>
            <th class="text-right font-medium px-3">Service p50</th>
            <th class="text-right font-medium px-3">Service p90</th>
            <th class="text-left font-medium px-3">Queue length</th>
            <th class="text-right font-medium px-4">Failed</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="l in lanes" :key="l.name" class="kt-v2-line border-b last:border-b-0 kt-row cursor-pointer" @click="run.view = 'machine'">
            <td class="px-4 h-10 font-medium">{{ l.name }}</td>
            <td class="px-3 text-right num">{{ l.units }}</td>
            <td class="px-3 text-right num">{{ rate(l.rate) }}</td>
            <td class="px-3 text-right num">{{ num(l.batch) }}</td>
            <td class="px-3 text-right num">{{ us(l.queue?.p50) }}</td>
            <td class="px-3 text-right num">{{ us(l.queue?.p90) }}</td>
            <td class="px-3 text-right num">{{ us(l.service?.p50) }}</td>
            <td class="px-3 text-right num">{{ us(l.service?.p90) }}</td>
            <td class="px-3"><div class="flex items-center gap-2"><Spark :values="l.queueLen" :label="`${l.name} queue`" /><span class="num text-warm-500">{{ count(l.queueLast) }}</span></div></td>
            <td class="px-4 text-right num" :class="l.errors ? 'text-coral-shadow dark:text-coral-light' : 'text-warm-400'">{{ l.errors ? count(l.errors) : "—" }}</td>
          </tr>
        </tbody>
      </table>
    </SectionCard>

    <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <SectionCard title="Slowest statements" icon="i-carbon-hourglass" hint="by service p99" flush>
        <StatementTable :rows="slowest" metric="service" @open="openStatement" />
      </SectionCard>
      <SectionCard title="Failures" icon="i-carbon-warning-alt" :count="failing.length" hint="statements with failed attempts" flush>
        <StatementTable v-if="failing.length" :rows="failing" metric="errors" @open="openStatement" />
        <p v-else class="px-4 py-4 text-xs text-warm-400 flex items-center gap-1.5"><span class="i-carbon-checkmark-filled text-sage" />No failed attempts in this range.</p>
      </SectionCard>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue"

import { api } from "@/api/client"
import ChartLegend from "@/components/charts/ChartLegend.vue"
import PhaseBar from "@/components/charts/PhaseBar.vue"
import Spark from "@/components/charts/Spark.vue"
import TimeChart from "@/components/charts/TimeChart.vue"
import EventFeed from "@/components/ui/EventFeed.vue"
import SectionCard from "@/components/ui/SectionCard.vue"
import StatTile from "@/components/ui/StatTile.vue"
import StatementTable from "@/components/ui/StatementTable.vue"
import { useLoad } from "@/composables/useLoad"
import { usePrograms } from "@/composables/usePrograms"
import { useLiveStore } from "@/stores/live"
import { useRunStore } from "@/stores/run"
import { count, num, pct, rate, us } from "@/utils/format"
import WaitToggle from "@/components/ui/WaitToggle.vue"
import { PHASE_LABEL, phase, series } from "@/utils/palette"
import { mean, statementRows, totalTime } from "@/utils/statements"

const run = useRunStore()
const live = useLiveStore()
const { index } = usePrograms()

const { data: nodeTable } = useLoad((r) => api.table(r.runId, "node", r.t0, r.t1), () => null, [])
const { data: entryTable } = useLoad((r) => api.table(r.runId, "entry", r.t0, r.t1), () => null, [])
const { data: laneTable } = useLoad((r) => api.table(r.runId, "lane", r.t0, r.t1), () => null, [])
const { data: unitTable } = useLoad((r) => api.table(r.runId, "unit", r.t0, r.t1), () => null, [])
const { data: samples } = useLoad((r) => api.samples(r.runId, { names: ["jobs/resident"], prefix: "lane/", t0: r.t0, t1: r.t1 }), () => null, {})

// The entry with the most jobs drives the throughput chart
const mainEntry = computed(() => [...(entryTable.value || [])].sort((a, b) => (b.metrics.admitted?.n ?? 0) - (a.metrics.admitted?.n ?? 0))[0])
const { data: tp } = useLoad(
  async (r) => {
    const e = mainEntry.value
    if (!e) return null
    const [adm, done] = await Promise.all([api.series(r.runId, e.entity, "admitted", r.t0, r.t1, 300, "rate"), api.series(r.runId, e.entity, "job", r.t0, r.t1, 300, "rate")])
    return { adm, done }
  },
  () => mainEntry.value?.entity,
)

const rows = computed(() => statementRows(nodeTable.value, index.value))

const jobs = computed(() => {
  const out = { done: 0, admitted: 0, failed: 0, p50: null, p90: null, p99: null }
  for (const e of entryTable.value || []) {
    const m = e.metrics
    out.done += m.job?.n ?? 0
    out.admitted += m.admitted?.n ?? 0
    out.failed += m.failed?.n ?? 0
  }
  const m = mainEntry.value?.metrics.job
  if (m) Object.assign(out, { p50: m.p50, p90: m.p90, p99: m.p99 })
  return out
})

const calls = computed(() => {
  const out = { n: 0, rate: 0, errors: 0, faults: 0 }
  for (const r of rows.value) {
    out.n += r.calls
    out.rate += r.rate
    out.errors += r.errors
    out.faults += r.faults
  }
  return out
})

const resident = computed(() => {
  const s = samples.value?.["jobs/resident"]
  const values = s?.v || []
  return { values, last: values[values.length - 1] ?? null, peak: values.length ? Math.max(...values) : null }
})

// Throughput: admitted and done on one x (the done series' buckets)
const throughput = computed(() => {
  const d = tp.value
  if (!d || !d.done.t.length) return { x: [], series: [] }
  const at = new Map(d.adm.t.map((t, i) => [t, d.adm.v[i]]))
  void run.theme
  return {
    x: d.done.t,
    series: [
      { label: "Admitted", values: d.done.t.map((t) => at.get(t) ?? 0), color: series(0) },
      { label: "Done", values: d.done.v, color: series(1), fill: true },
    ],
  }
})
const throughputTrend = computed(() => throughput.value.series[1]?.values || null)
const throughputLegend = computed(() => throughput.value.series.map((s) => ({ label: s.label, color: s.color })))
const phaseLegend = computed(() => (void run.theme, run.phases.map((p) => ({ label: PHASE_LABEL[p], color: phase(p) }))))
const rateFmt = (v) => `${rate(v)}/s`

// Where the time goes: statements by total time, bars by mean time per call
const timeRows = computed(() => {
  const phases = run.phases
  const all = rows.value.map((r) => ({ ...r, time: totalTime(r, phases), perCall: phases.reduce((t, p) => t + mean(r, p), 0) }))
  const total = all.reduce((t, r) => t + r.time, 0) || 1
  return all
    .filter((r) => r.time > 0)
    .sort((a, b) => b.time - a.time)
    .slice(0, 8)
    .map((r) => ({ ...r, share: r.time / total }))
})
const maxPerCall = computed(() => Math.max(1, ...timeRows.value.map((r) => r.perCall)))

const lanes = computed(() => {
  const built = run.info?.machine?.lanes || []
  const units = new Map(built.map((l) => [l.name, l.units.length]))

  // A lane's failed attempts are its units' (results carry the unit)
  const laneOf = new Map(built.flatMap((l) => l.units.map((u) => [u, l.name])))
  const errors = new Map()
  for (const u of unitTable.value || []) {
    const lane = laneOf.get(run.unitName(u.id))
    if (lane) errors.set(lane, (errors.get(lane) ?? 0) + (u.metrics.errors?.n ?? 0))
  }
  return (laneTable.value || []).map((l) => {
    const name = run.laneName(l.id)
    const m = l.metrics
    const q = samples.value?.[`lane/${name}/queue`]?.v || []
    return {
      name,
      units: units.get(name) ?? "—",
      rate: m.service?.rate ?? m.queue?.rate ?? 0,
      batch: m.batch?.mean ?? null,
      queue: m.queue,
      service: m.service,
      errors: errors.get(name) ?? 0,
      queueLen: q,
      queueLast: q[q.length - 1] ?? null,
    }
  })
})

const slowest = computed(() => [...rows.value].filter((r) => r.service?.p99).sort((a, b) => b.service.p99 - a.service.p99).slice(0, 8))
const failing = computed(() => [...rows.value].filter((r) => r.errors || r.faults).sort((a, b) => b.errors - a.errors).slice(0, 8))

function openStatement(r) {
  run.selected = r.path || null
  run.view = "program"
}
</script>
