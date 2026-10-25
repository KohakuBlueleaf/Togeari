<template>
  <div class="h-full flex min-h-0" :class="compact ? 'flex-col' : ''">
    <div class="flex-1 min-w-0 flex flex-col gap-3 p-4" :class="compact ? 'overflow-auto' : 'min-h-0'">
      <!-- The machine, left to right as one job's statements travel; on a phone it scrolls sideways -->
      <div :class="compact ? 'overflow-x-auto shrink-0 -mx-4 px-4' : 'flex-1 min-h-0 flex flex-col'">
      <div class="grid gap-y-2 kt-machine" :class="compact ? 'min-w-[860px] h-[620px]' : 'flex-1 min-h-0'" :style="{ gridTemplateColumns: COLUMNS, gridTemplateRows: 'auto minmax(0, 1fr)' }">
        <ColumnHead title="many jobs" sub="each a thread of the program" />
        <div />
        <ColumnHead title="Dispatcher" sub="one thread, out of order" accent />
        <div />
        <ColumnHead title="lanes → units" sub="queue, batch, pick a unit" />

        <!-- Jobs: source, admission, the pool, the sink -->
        <div class="flex flex-col gap-2 min-h-0">
          <Block title="Source" icon="i-carbon-data-base" :on="run.part === 'jobs'" @pick="pick('jobs')">
            <Stat :value="rate(m.jobs.admittedRate)" unit="jobs/s" :note="`${count(m.jobs.admitted)} admitted`" />
          </Block>
          <Block title="Admission" icon="i-carbon-filter" :on="run.part === 'jobs'" @pick="pick('jobs')">
            <div class="text-[10.5px] text-warm-500 truncate" :title="policyText(m.admission)">{{ policyText(m.admission) }}</div>
            <Stat :value="num(m.pool.mean, 1)" unit="resident" :note="`peak ${count(m.pool.peak)}`" />
          </Block>
          <Block title="Job pool" icon="i-carbon-layers" class="flex-1" :on="run.part === 'pool'" @pick="pick('pool')">
            <div class="flex flex-col gap-1.5 mt-1">
              <div v-for="s in m.pool.states" :key="s.key" class="grid grid-cols-[4.2rem_minmax(0,1fr)] items-center gap-2 text-[11px]" :title="s.note">
                <span class="text-warm-600 dark:text-warm-300">{{ s.key }}</span>
                <FillBar :value="m.pool.mean ? (s.mean ?? 0) / m.pool.mean : null" :label="num(s.mean, 1)" />
              </div>
            </div>
            <Spark class="mt-auto pt-2" :values="m.pool.trend" :width="220" :height="26" label="resident jobs" />
          </Block>
          <Block title="Sink" icon="i-carbon-checkmark-outline" accent="sink" :on="run.part === 'jobs'" @pick="pick('jobs')">
            <Stat :value="rate(m.jobs.doneRate)" unit="jobs/s" :note="`p50 ${us(m.jobs.p50)}`" />
            <span v-if="m.jobs.failed" class="chip-coral self-start mt-1"><span class="i-carbon-error-filled text-[10px]" />{{ count(m.jobs.failed) }} failed</span>
          </Block>
        </div>
        <Arrow />

        <!-- The dispatcher: how busy, how fast, where its time goes -->
        <Block title="Dispatcher" accent="dispatcher" :sub="`${run.info?.machine?.core ?? ''} core`" class="min-h-0" :on="run.part === 'dispatcher'" @pick="pick('dispatcher')">
          <div class="flex flex-wrap gap-x-4 gap-y-1 mt-1">
            <Stat :value="pct(m.dispatcher.busy, 0)" unit="busy" />
            <Stat :value="rate(m.dispatcher.cycles)" unit="cycles/s" />
            <Stat :value="rate(m.dispatcher.calls)" unit="calls/s" />
          </div>
          <FillBar class="mt-2" :value="m.dispatcher.busy" :label="null" title="busy: time outside the inbox wait" />
          <div class="mt-3 section-title">Time per phase</div>
          <div class="mt-1 flex flex-col gap-2 min-h-0">
            <div v-for="p in m.dispatcher.phases" :key="p.key" class="min-w-0">
              <div class="flex items-baseline gap-2 text-[11.5px]">
                <span class="font-medium">{{ p.label }}</span>
                <span class="ml-auto num text-[10.5px] text-warm-500">{{ pct(p.share, 1) }}</span>
              </div>
              <div class="text-[10.5px] text-warm-400 truncate">{{ p.note }}</div>
              <FillBar class="mt-0.5" :value="p.share / m.dispatcher.phaseTop" :label="null" />
            </div>
          </div>
          <p class="mt-auto pt-2 text-[10.5px] text-warm-400 leading-snug">A statement issues once its inputs are filled, in any order; results still retire in program order.</p>
        </Block>
        <Arrow />

        <!-- Lanes and their units -->
        <div class="flex flex-col gap-2 min-h-0 overflow-auto pr-0.5">
          <Block title="INLINE" sub="on the dispatcher, no handoff" class="shrink-0" :on="run.part === 'dispatcher'" @pick="pick('dispatcher')">
            <template #aside><span class="num text-[11px] text-warm-500">{{ rate(inline.rate) }}/s · {{ us(inline.mean) }}</span></template>
          </Block>
          <div v-for="l in m.lanes" :key="l.name" class="grid gap-2 shrink-0" style="grid-template-columns: minmax(0, 1fr) 14px minmax(0, 1.1fr)">
            <Block :title="l.name" :sub="l.stream ? 'stream lane · paces only' : `${l.kind} lane`" icon="i-carbon-flow-stream" :on="run.part === `lane:${l.name}`" @pick="pick(`lane:${l.name}`)">
              <template #aside>
                <span v-if="l.gatesAdmission" class="chip-iolite" title="this lane's load gates admission"><span class="i-carbon-arrow-left text-[10px]" />admission</span>
                <span v-if="l.errors" class="chip-amber"><span class="i-carbon-warning-alt-filled text-[10px]" />{{ count(l.errors) }}</span>
              </template>
              <div class="flex items-center gap-1.5 mt-0.5" :title="`queued in the lane: mean ${num(l.queue.mean, 1)}, peak ${count(l.queue.peak)}; waiting inside the engines: mean ${num(l.engineQueue.mean ?? 0, 1)}`">
                <span v-for="i in SQUARES" :key="i" class="w-2.5 h-2.5 rounded-[2px]" :style="{ background: i <= queued(l) ? queueColor : 'var(--v2-line)' }" />
                <span class="num text-[10.5px] text-warm-500 ml-1 truncate">{{ num(l.waiting.mean, 1) }} waiting<template v-if="l.engineQueue.mean"> ({{ num(l.engineQueue.mean, 1) }} in the engine)</template></span>
              </div>
              <div class="mt-1.5 text-[10.5px] text-warm-500 flex items-center gap-1">fill <span class="text-warm-400">(in flight / capacity {{ count(l.capacity) }})</span></div>
              <FillBar :value="l.fill" :color="serviceColor" :label="pct(l.fill, 0)" />
              <div class="mt-1 num text-[10.5px] text-warm-500 truncate">{{ rate(l.items) }}/s · batch {{ num(l.batch, 1) }} · queue p90 {{ us(l.queueP90) }}</div>
            </Block>
            <Arrow small />
            <Block :title="`${l.units.length} unit${l.units.length === 1 ? '' : 's'}`" icon="i-carbon-chip" :on="l.units.some((u) => run.part === `unit:${u.name}`)" @pick="pick(`unit:${l.units[0]?.name}`)">
              <div class="flex flex-col gap-1 mt-0.5">
                <div
                  v-for="u in l.units"
                  :key="u.name"
                  class="grid grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_2.6rem] items-center gap-2 text-[11px] rounded px-1 -mx-1 kt-row"
                  :title="u.name"
                  :class="run.part === `unit:${u.name}` ? 'kt-row--on' : ''"
                  @click.stop="pick(`unit:${u.name}`)"
                >
                  <span class="truncate flex items-center gap-1"><span :class="UNIT_STATE[u.state].icon" :style="{ color: stateColor(u.state) }" class="text-[10px] shrink-0" />{{ u.name }}</span>
                  <FillBar :value="u.fill" :color="serviceColor" :label="pct(u.fill, 0)" :title="`fill: held / (held + free), capacity ${count(u.capacity)}`" />
                  <span class="num text-[10px] text-warm-500 text-right" title="busy">{{ pct(u.busy, 0) }}</span>
                </div>
              </div>
            </Block>
          </div>
          <p class="text-[11px] text-iolite dark:text-iolite-light flex items-center gap-1.5 px-1 shrink-0"><span class="i-carbon-arrow-left" />results from every unit → the dispatcher's inbox → writeback</p>
        </div>
      </div>
      </div>

      <!-- In flight per lane: small multiples on one cursor -->
      <div class="shrink-0 grid gap-2" :style="{ gridTemplateColumns: compact ? 'minmax(0, 1fr)' : `repeat(${Math.max(1, m.lanes.length)}, minmax(0, 1fr))` }">
        <div v-for="l in m.lanes" :key="l.name" class="kt-v2-card px-3 pt-2 pb-1 min-w-0">
          <div class="flex items-center gap-2 text-[11px] mb-0.5">
            <span class="font-semibold">{{ l.name }}</span>
            <span class="text-warm-400">{{ l.kind }}</span>
            <ChartLegend class="ml-auto" :items="strip.legend" />
          </div>
          <TimeChart :x="strip.lanes[l.name].x" :series="strip.lanes[l.name].series" :format="(v) => count(v)" :height="96" stepped integer />
        </div>
      </div>
    </div>

    <SidePane v-if="run.part" name="machine" label="Details" icon="i-carbon-information" :initial="400">
      <MachinePanel :model="m" :samples="samples" />
    </SidePane>
  </div>
</template>

<script setup>
import { computed, h } from "vue"

import { api } from "@/api/client"
import ChartLegend from "@/components/charts/ChartLegend.vue"
import FillBar from "@/components/charts/FillBar.vue"
import Spark from "@/components/charts/Spark.vue"
import TimeChart from "@/components/charts/TimeChart.vue"
import SidePane from "@/components/shell/SidePane.vue"
import { useCompact } from "@/composables/useCompact"
import { useLoad } from "@/composables/useLoad"
import { useRunStore } from "@/stores/run"
import { count, num, pct, rate, us } from "@/utils/format"
import { phase, status } from "@/utils/palette"
import Block from "@/views/machine/Block.vue"
import MachinePanel from "@/views/machine/MachinePanel.vue"
import { machineModel, policyText, UNIT_STATE } from "@/views/machine/model"

const COLUMNS = "minmax(150px, 0.85fr) 18px minmax(220px, 1.15fr) 18px minmax(0, 2.5fr)"
const SQUARES = 10
const PREFIXES = ["dispatcher/", "jobs/", "lane/", "unit/"]

const run = useRunStore()
const compact = useCompact()

const { data: samples } = useLoad(
  async (r) => Object.assign({}, ...(await Promise.all(PREFIXES.map((prefix) => api.samples(r.runId, { prefix, t0: r.t0, t1: r.t1 }))))),
  () => null,
  {},
)
const { data: laneTable } = useLoad((r) => api.table(r.runId, "lane", r.t0, r.t1), () => null, [])
const { data: unitTable } = useLoad((r) => api.table(r.runId, "unit", r.t0, r.t1), () => null, [])
const { data: entryTable } = useLoad((r) => api.table(r.runId, "entry", r.t0, r.t1), () => null, [])
const { data: nodeTable } = useLoad((r) => api.table(r.runId, "node", r.t0, r.t1), () => null, [])

const byName = (table, nameOf) => new Map((table || []).map((r) => [nameOf(r.id), r.metrics]))
const m = computed(() =>
  machineModel(samples.value, run.info?.machine, {
    lane: byName(laneTable.value, run.laneName),
    unit: byName(unitTable.value, run.unitName),
    entry: entryTable.value || [],
  }),
)

// Inline work: statements that ran without a lane (no queue phase)
const inline = computed(() => {
  let n = 0
  let r = 0
  let t = 0
  for (const row of nodeTable.value || []) {
    const mm = row.metrics
    if (mm.queue || !mm.calls) continue
    r += mm.calls.rate ?? 0
    n += mm.service?.n ?? 0
    t += (mm.service?.mean ?? 0) * (mm.service?.n ?? 0)
  }
  return { rate: r, mean: n ? t / n : null }
})

const queueColor = computed(() => (void run.theme, phase("queue")))
const serviceColor = computed(() => (void run.theme, phase("service")))
const stateColor = (s) => (void run.theme, status({ up: "good", errors: "warn", down: "bad" }[s] || "idle"))
// Squares: what waits for the lane's units, in the lane's queue or inside the engines
const queued = (l) => Math.min(SQUARES, Math.round(l.waiting.mean ?? 0))

const strip = computed(() => {
  void run.theme
  const out = {}
  for (const l of m.value.lanes) {
    const q = l.waiting.series
    const f = l.inflight.series
    out[l.name] = q
      ? {
          x: q.t,
          series: [
            { label: "Waiting", values: q.v, color: phase("queue"), fill: true },
            ...(f ? [{ label: "In flight", values: f.v.slice(0, q.v.length), color: phase("service") }] : []),
          ],
        }
      : { x: [], series: [] }
  }
  return {
    lanes: out,
    legend: [
      { label: "Waiting", color: phase("queue") },
      { label: "In flight", color: phase("service") },
    ],
  }
})

function pick(part) {
  run.part = run.part === part ? null : part
}

// Small pieces of the picture
function ColumnHead(props) {
  return h("div", { class: "flex items-baseline gap-2 min-w-0 px-0.5" }, [
    h("span", { class: ["text-[14px] font-semibold", props.accent ? "text-iolite dark:text-iolite-light" : ""] }, props.title),
    h("span", { class: "text-[11px] text-warm-400 truncate" }, props.sub),
  ])
}
ColumnHead.props = ["title", "sub", "accent"]

function Arrow(props) {
  return h("div", { class: "flex items-center justify-center text-warm-400" }, [h("span", { class: ["i-carbon-arrow-right", props.small ? "text-[12px]" : "text-[15px]"] })])
}
Arrow.props = ["small"]

function Stat(props) {
  return h("div", { class: "min-w-0" }, [
    h("div", { class: "flex items-baseline gap-1" }, [
      h("span", { class: "num text-[17px] font-semibold leading-6" }, props.value),
      h("span", { class: "text-[10.5px] text-warm-500" }, props.unit),
    ]),
    props.note ? h("div", { class: "num text-[10.5px] text-warm-400 truncate" }, props.note) : null,
  ])
}
Stat.props = ["value", "unit", "note"]
</script>
