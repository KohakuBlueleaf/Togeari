<template>
  <div class="p-3 md:p-5 flex flex-col gap-4 max-w-[1600px] mx-auto">
    <p class="text-xs text-warm-500 flex items-center gap-1.5 flex-wrap">
      <span class="i-carbon-information" />Every chart shares one cursor; drag across any of them to narrow the range, double-click to go back one zoom.
      <span v-if="live.live" class="flex items-center gap-1.5"><span class="inline-block w-4 border-t-2 border-dashed border-warm-400" />machine events since the page opened (hover a rule)</span>
    </p>

    <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <SectionCard title="Jobs done" icon="i-carbon-checkmark-outline" hint="per second">
        <TimeChart :x="jobs.x" :series="jobs.series" :format="rateFmt" :height="150" :marks="marks" />
      </SectionCard>
      <SectionCard title="Resident jobs" icon="i-carbon-layers" hint="sampled">
        <TimeChart :x="resident.x" :series="resident.series" :format="(v) => count(v)" :height="150" stepped integer :marks="marks" />
      </SectionCard>
    </div>

    <SectionCard v-for="l in lanes" :key="l.id" :title="l.name" icon="i-carbon-flow-stream" hint="lane" flush>
      <div class="grid grid-cols-1 lg:grid-cols-3">
        <div class="p-4 kt-v2-line lg:border-r">
          <div class="section-title mb-1.5">Items per second</div>
          <TimeChart :x="l.rate.x" :series="l.rate.series" :format="rateFmt" :height="130" :marks="laneMarks[l.name] || marks" />
        </div>
        <div class="p-4 kt-v2-line lg:border-r">
          <div class="section-title mb-1.5">Queue length</div>
          <TimeChart :x="l.queue.x" :series="l.queue.series" :format="(v) => count(v)" :height="130" stepped integer :marks="laneMarks[l.name] || marks" />
        </div>
        <div class="p-4">
          <div class="section-title mb-1.5">Service p90 <span class="normal-case tracking-normal text-warm-400">· band: min–max</span></div>
          <TimeChart :x="l.p90.x" :series="l.p90.series" :format="us" :height="130" :marks="laneMarks[l.name] || marks" />
        </div>
      </div>
    </SectionCard>

    <SectionCard title="Telemetry" icon="i-carbon-meter" hint="the writer's own cost" flush>
      <div class="grid grid-cols-1 lg:grid-cols-3">
        <div class="p-4 kt-v2-line lg:border-r">
          <div class="section-title mb-1.5">Writer tick</div>
          <TimeChart :x="health.tick.x" :series="health.tick.series" :format="(v) => `${num(v, 1)} ms`" :height="120" />
        </div>
        <div class="p-4 kt-v2-line lg:border-r">
          <div class="section-title mb-1.5">Records pending in the ring</div>
          <TimeChart :x="health.pending.x" :series="health.pending.series" :format="(v) => count(v)" :height="120" stepped integer />
        </div>
        <div class="p-4">
          <div class="section-title mb-1.5">Records dropped (total)</div>
          <TimeChart :x="health.dropped.x" :series="health.dropped.series" :format="(v) => count(v)" :height="120" stepped integer />
        </div>
      </div>
    </SectionCard>
  </div>
</template>

<script setup>
import { computed } from "vue"

import { api } from "@/api/client"
import TimeChart from "@/components/charts/TimeChart.vue"
import SectionCard from "@/components/ui/SectionCard.vue"
import { useLoad } from "@/composables/useLoad"
import { RARE, useLiveStore } from "@/stores/live"
import { useRunStore } from "@/stores/run"
import { count, num, rate, us } from "@/utils/format"
import { phase, series, status } from "@/utils/palette"

const POINTS = 300
const run = useRunStore()
const live = useLiveStore()
const rateFmt = (v) => `${rate(v)}/s`

const { data: entries } = useLoad((r) => api.table(r.runId, "entry", r.t0, r.t1), () => null, [])
const { data: jobSeries } = useLoad(
  async (r) => {
    const out = await Promise.all((entries.value || []).map((e) => api.series(r.runId, e.entity, "job", r.t0, r.t1, POINTS, "rate")))
    return out
  },
  () => (entries.value || []).map((e) => e.entity).join(","),
)
const { data: samples } = useLoad(
  (r) => api.samples(r.runId, { names: ["jobs/resident", "writer/tick_ms", "ring/pending", "ring/dropped"], prefix: "lane/", t0: r.t0, t1: r.t1 }),
  () => null,
  {},
)
const { data: laneSeries } = useLoad(
  async (r) => {
    const lanes = (r.info?.names || []).filter((n) => n.kind === "lane")
    return Promise.all(
      lanes.map(async (l) => {
        const e = `lane:${l.id}`
        const [rateS, p90] = await Promise.all([api.series(r.runId, e, "service", r.t0, r.t1, POINTS, "rate"), api.series(r.runId, e, "service", r.t0, r.t1, POINTS, "p90")])
        return { id: l.id, name: l.name, rate: rateS, p90 }
      }),
    )
  },
  () => null,
  [],
)

const line = (s, label, color, opts = {}) => (s?.t?.length ? { x: s.t, series: [{ label, values: s.v, color, ...opts }] } : { x: [], series: [] })

const jobs = computed(() => {
  void run.theme
  const all = jobSeries.value || []
  if (!all.length || !all[0].t.length) return { x: [], series: [] }
  // Entries add up on the first entry's buckets
  const x = all[0].t
  const sum = x.map((t, i) => all.reduce((acc, s) => acc + (s.t[i] === t ? (s.v[i] ?? 0) : 0), 0))
  return { x, series: [{ label: "jobs/s", values: sum, color: series(1), fill: true }] }
})
const resident = computed(() => (void run.theme, line(samples.value?.["jobs/resident"], "resident", series(0), { fill: true })))

// Rare events of the live machine as rules: every event on the job charts; on a lane's charts,
// the events naming that lane or one of its units, and the machine-wide ones
const marks = computed(() => {
  void run.theme
  return live.events.map((e) => ({ t: e.t, label: [e.kind, e.unit ?? e.lane ?? e.plugin ?? e.error ?? ""].join(" "), color: status(RARE[e.kind] || "idle"), e }))
})
const unitsOf = computed(() => new Map((run.info?.machine?.lanes || []).map((l) => [l.name, new Set(l.units)])))
const laneMarks = computed(() => {
  const out = {}
  for (const [name, units] of unitsOf.value) out[name] = marks.value.filter(({ e }) => (e.lane === undefined && e.unit === undefined) || e.lane === name || units.has(e.unit))
  return out
})

const lanes = computed(() => {
  void run.theme
  return (laneSeries.value || []).map((l) => ({
    id: l.id,
    name: l.name,
    rate: line(l.rate, "items/s", series(0), { fill: true }),
    queue: line(samples.value?.[`lane/${l.name}/queue`], "queued", phase("queue"), { fill: true }),
    p90: l.p90?.t?.length ? { x: l.p90.t, series: [{ label: "p90", values: l.p90.v, color: phase("service"), band: [l.p90.lo, l.p90.hi] }] } : { x: [], series: [] },
  }))
})

const health = computed(() => {
  void run.theme
  const s = samples.value || {}
  return {
    tick: line(s["writer/tick_ms"], "tick", series(0)),
    pending: line(s["ring/pending"], "pending", series(1)),
    dropped: line(s["ring/dropped"], "dropped", status("bad")),
  }
})
</script>
