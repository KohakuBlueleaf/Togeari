<template>
  <div class="flex flex-col">
    <!-- Head: what is open -->
    <div class="px-5 pt-4 pb-3 kt-v2-line border-b">
      <div class="flex items-center gap-2">
        <span :class="head.icon" class="text-warm-500 text-[15px]" />
        <span class="text-[15px] font-semibold truncate">{{ head.title }}</span>
        <span v-if="head.chip" class="chip-warm">{{ head.chip }}</span>
        <button class="btn-icon ml-auto" title="Close" @click="run.part = null"><span class="i-carbon-close" /></button>
      </div>
      <p v-if="head.note" class="mt-0.5 text-[11px] text-warm-500">{{ head.note }}</p>
      <div v-if="lane" class="mt-2 flex flex-wrap gap-1">
        <span v-for="c in configChips(lane.config)" :key="c" class="chip-warm">{{ c }}</span>
      </div>
    </div>

    <div class="grid grid-cols-3 gap-px kt-v2-line border-b bg-[var(--v2-line)]">
      <div v-for="t in tiles" :key="t.label" class="bg-[var(--v2-panel)] px-4 py-2.5 min-w-0">
        <div class="section-title truncate">{{ t.label }}</div>
        <div class="num text-[15px] font-semibold mt-0.5 truncate" :class="t.tone">{{ t.value }}</div>
      </div>
    </div>

    <!-- Lane, live: its former and selector, swapped for contributed policies -->
    <div v-if="lane && liveLane" class="px-5 py-3.5 kt-v2-line border-b">
      <div class="section-title mb-1.5 flex items-center gap-1.5">Policies <span class="chip-sage !text-[10px]">live</span></div>
      <div v-for="p in lanePolicies" :key="p.role" class="flex items-center gap-2 py-1 text-[11px] min-w-0">
        <span class="text-warm-500 w-12 shrink-0">{{ p.role }}</span>
        <span class="num truncate">{{ p.current }}</span>
        <select class="kt-input ml-auto w-36 shrink-0" :disabled="!p.choices.length" :title="p.choices.length ? `Swap the ${p.role} for a contributed policy` : 'No plugin contributes one'" @change="proposePolicy(p, $event)">
          <option value="" selected>{{ p.choices.length ? "swap for…" : "none contributed" }}</option>
          <option v-for="c in p.choices" :key="c" :value="c">{{ c }}</option>
        </select>
      </div>
    </div>

    <!-- Unit, live: its state and detaching it -->
    <div v-if="unit && liveUnit" class="px-5 py-2.5 kt-v2-line border-b flex items-center gap-2 text-[11px]">
      <span class="chip-sage !text-[10px]">live</span>
      <span class="text-warm-500">state</span><span class="num font-medium">{{ liveUnit.state }}</span>
      <span class="text-warm-500 ml-2">in flight</span><span class="num">{{ count(liveUnit.inflight) }}</span>
      <button class="btn-ghost ml-auto !text-coral-shadow dark:!text-coral-light" :disabled="liveUnit.state === 'detached'" @click="proposeDetach">
        <span class="i-carbon-unlink" />Detach
      </button>
    </div>

    <!-- Charts of the open part -->
    <div v-for="c in charts" :key="c.title" class="px-5 py-3.5 kt-v2-line border-b">
      <div class="flex items-center mb-1.5">
        <span class="section-title">{{ c.title }}</span>
        <ChartLegend v-if="c.series.length > 1" class="ml-auto" :items="c.series.map((s) => ({ label: s.label, color: s.color }))" />
      </div>
      <TimeChart :x="c.x" :series="c.series" :format="c.format" :height="c.height || 120" :stepped="!!c.integer" :integer="!!c.integer" />
    </div>

    <!-- Dispatcher: time per phase -->
    <div v-if="kind === 'dispatcher'" class="px-5 py-3.5 kt-v2-line border-b">
      <div class="section-title mb-2">Time per phase (share of wall time)</div>
      <table class="w-full text-xs">
        <tbody>
          <tr v-for="p in model.dispatcher.phases" :key="p.key" :title="p.note">
            <td class="py-1 font-medium">{{ p.label }}</td>
            <td class="py-1 text-warm-500 text-[11px]">{{ p.note }}</td>
            <td class="py-1 text-right num">{{ pct(p.share, 1) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Pool: jobs by state, small multiples -->
    <div v-if="kind === 'pool'" class="px-5 py-3.5 kt-v2-line border-b grid grid-cols-2 gap-x-3 gap-y-2">
      <div v-for="s in model.pool.states" :key="s.key" class="min-w-0">
        <div class="flex items-baseline gap-1.5 text-[11px]" :title="s.note">
          <span class="font-medium">{{ s.key }}</span><span class="ml-auto num text-warm-500">mean {{ num(s.mean, 1) }}</span>
        </div>
        <TimeChart :x="s.series?.t || []" :series="s.series ? [{ label: s.key, values: s.series.v, color: seriesColor(0), fill: true }] : []" :format="(v) => count(v)" :height="80" stepped integer />
      </div>
    </div>

    <!-- Lane: its units -->
    <div v-if="lane" class="py-2 kt-v2-line border-b">
      <table class="w-full text-xs">
        <thead class="text-warm-400">
          <tr>
            <th class="text-left font-medium px-5 h-7">Unit</th>
            <th class="text-right font-medium px-2">Items / s</th>
            <th class="text-right font-medium px-2">Fill</th>
            <th class="text-right font-medium px-2">Busy</th>
            <th class="text-right font-medium px-5">p90</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in lane.units" :key="u.name" class="kt-row cursor-pointer" @click="run.part = `unit:${u.name}`">
            <td class="px-5 h-8">
              <span :class="UNIT_STATE[u.state].icon" :style="{ color: stateColor(u.state) }" class="text-[10px] mr-1" />{{ u.name }}
            </td>
            <td class="px-2 text-right num">{{ rate(u.items) }}</td>
            <td class="px-2 text-right num">{{ pct(u.fill, 0) }}</td>
            <td class="px-2 text-right num">{{ pct(u.busy, 0) }}</td>
            <td class="px-5 text-right num">{{ us(u.serviceP90) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Unit: what the engine reports, and its service distribution -->
    <template v-if="unit">
      <div v-if="engine.length" class="px-5 py-3.5 kt-v2-line border-b">
        <div class="section-title mb-1.5">The engine's own numbers (last sample)</div>
        <div class="flex flex-wrap gap-1">
          <span v-for="e in engine" :key="e.k" class="chip-warm">{{ e.k }} <b class="num font-medium">{{ e.v }}</b></span>
        </div>
      </div>
      <div class="px-5 py-3.5">
        <div class="section-title mb-1.5">Service distribution</div>
        <HistChart :hist="hist" phase="service" />
      </div>
    </template>

    <!-- Jobs: entries -->
    <div v-if="kind === 'jobs'" class="py-2">
      <table class="w-full text-xs">
        <thead class="text-warm-400">
          <tr>
            <th class="text-left font-medium px-5 h-7">Entry</th>
            <th class="text-right font-medium px-2">Admitted</th>
            <th class="text-right font-medium px-2">Failed</th>
            <th class="text-right font-medium px-5">Job p50</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in entries" :key="e.entity">
            <td class="px-5 h-8 num">@{{ e.id }}</td>
            <td class="px-2 text-right num">{{ count(e.metrics.admitted?.n) }}</td>
            <td class="px-2 text-right num">{{ count(e.metrics.failed?.n ?? 0) }}</td>
            <td class="px-5 text-right num">{{ us(e.metrics.job?.p50) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Dispatcher: the machine's knobs (editable while live) and plugins -->
    <div v-if="kind === 'dispatcher'" class="px-5 py-3.5" :class="live.live ? 'kt-v2-line border-b' : ''">
      <div class="section-title mb-1.5 flex items-center gap-1.5">Knobs <span v-if="specs" class="chip-sage !text-[10px]">live</span></div>
      <template v-if="specs">
        <KnobRow v-for="(s, k) in specs.knobs" :key="k" :name="k" :spec="s" :policies="specs.policies" @propose="(v) => proposeKnob(k, s, v)" />
      </template>
      <template v-else>
        <div v-for="[k, v] in knobs" :key="k" class="flex items-baseline gap-2 py-0.5 text-[11px] min-w-0">
          <span class="text-warm-500 truncate">{{ k }}</span>
          <span class="ml-auto num truncate" :title="String(v)">{{ v }}</span>
        </div>
      </template>
    </div>
    <div v-if="kind === 'dispatcher' && plugins.length" class="px-5 py-3.5">
      <div class="section-title mb-1.5 flex items-center gap-1.5">Plugins <span class="chip-sage !text-[10px]">live</span></div>
      <div v-for="p in plugins" :key="p.name" class="flex items-center gap-2 py-1 text-[11px] min-w-0">
        <span class="truncate" :class="p.enabled ? '' : 'text-warm-400'">{{ p.name }}</span>
        <span v-if="p.errors" class="chip-amber !text-[10px]">{{ count(p.errors) }} errors</span>
        <span class="ml-auto num text-[10px] text-warm-400" title="total time in its hooks on the dispatcher">{{ us(hookUs(p)) }}</span>
        <button class="kt-switch shrink-0" :class="p.enabled ? 'kt-switch--on' : ''" :title="p.enabled ? `Disable ${p.name}` : `Enable ${p.name}`" @click="proposePlugin(p)" />
      </div>
    </div>

    <ConfirmDialog :ask="ask" @close="ask = null" />
  </div>
</template>

<script setup>
import { computed, ref } from "vue"

import { api } from "@/api/client"
import ChartLegend from "@/components/charts/ChartLegend.vue"
import HistChart from "@/components/charts/HistChart.vue"
import TimeChart from "@/components/charts/TimeChart.vue"
import ConfirmDialog from "@/components/ui/ConfirmDialog.vue"
import { useLoad } from "@/composables/useLoad"
import { useLiveStore } from "@/stores/live"
import { useRunStore } from "@/stores/run"
import { count, num, pct, rate, us } from "@/utils/format"
import { phase, series as seriesColor, status } from "@/utils/palette"
import KnobRow from "@/views/machine/KnobRow.vue"
import { configChips, perSecond, UNIT_STATE } from "@/views/machine/model"

/** The side panel of the Machine view: the open part's numbers, charts and tables. */
const props = defineProps({
  model: { type: Object, required: true },
  samples: { type: Object, default: () => ({}) },
})

const run = useRunStore()
const kind = computed(() => (run.part || "").split(":")[0])
const name = computed(() => (run.part || "").split(":").slice(1).join(":"))
const lane = computed(() => (kind.value === "lane" ? props.model.lanes.find((l) => l.name === name.value) : null))
const unit = computed(() => {
  if (kind.value !== "unit") return null
  for (const l of props.model.lanes) for (const u of l.units) if (u.name === name.value) return { ...u, lane: l.name }
  return null
})
const S = (n) => props.samples?.[n] || null
const stateColor = (s) => (void run.theme, status({ up: "good", errors: "warn", down: "bad" }[s] || "idle"))
const one = (label, s, color, extra = {}) => (s ? { label, values: s.v, color, ...extra } : null)

/** Series sampled together, summed index by index (null when none). */
function sumOf(list) {
  const got = list.filter(Boolean)
  if (!got.length) return null
  const n = Math.min(...got.map((s) => s.v.length))
  return { t: got[0].t.slice(0, n), v: Array.from({ length: n }, (_, i) => got.reduce((a, s) => a + s.v[i], 0)) }
}

const { data: entryTable } = useLoad((r) => (kind.value === "jobs" ? api.table(r.runId, "entry", r.t0, r.t1) : null), () => kind.value, [])
const entries = computed(() => entryTable.value || [])
const mainEntry = computed(() => [...entries.value].sort((a, b) => (b.metrics.admitted?.n ?? 0) - (a.metrics.admitted?.n ?? 0))[0])
const { data: tp } = useLoad(
  async (r) => {
    const e = mainEntry.value
    if (kind.value !== "jobs" || !e) return null
    const [adm, done] = await Promise.all([api.series(r.runId, e.entity, "admitted", r.t0, r.t1, 300, "rate"), api.series(r.runId, e.entity, "job", r.t0, r.t1, 300, "rate")])
    return { adm, done }
  },
  () => [kind.value, mainEntry.value?.entity],
)

const unitId = computed(() => {
  if (!unit.value) return null
  for (const [id, row] of run.names.unit) if (row.name === unit.value.name) return id
  return null
})
const { data: hist } = useLoad((r) => (unitId.value === null ? null : api.hist(r.runId, `unit:${unitId.value}`, "service", r.t0, r.t1)), () => unitId.value)

const OWN = new Set(["held", "free", "busy_s", "done", "running", "waiting"])
const engine = computed(() => {
  if (!unit.value) return []
  const prefix = `unit/${unit.value.name}/`
  return Object.entries(props.samples || {})
    .filter(([k, s]) => k.startsWith(prefix) && !OWN.has(k.slice(prefix.length)) && s.v.length)
    .map(([k, s]) => ({ k: k.slice(prefix.length), v: num(s.v[s.v.length - 1], 0) }))
})
const knobs = computed(() => Object.entries(run.info?.machine?.knobs || {}).map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : v]))

// The live machine: its answers this second, and the command each control proposes
const live = useLiveStore()
const ask = ref(null)
const specs = computed(() => (live.live ? live.now.knob_specs || null : null))
const plugins = computed(() => (live.live ? live.now.plugins || [] : []))
const liveLane = computed(() => (live.live && lane.value ? (live.now.lanes || []).find((l) => l.name === lane.value.name) || null : null))
const liveUnit = computed(() => {
  if (!live.live || !unit.value) return null
  for (const l of live.now.lanes || []) for (const u of l.units) if (u.name === unit.value.name) return u
  return null
})
const lanePolicies = computed(() => {
  const l = liveLane.value
  if (!l) return []
  const by = live.now.knob_specs?.policies || {}
  const out = [{ role: "select", current: l.select, choices: by.select || [] }]
  if (l.former) out.unshift({ role: "former", current: l.former, choices: by.former || [] })
  return out
})
const hookUs = (p) => Object.values(p.hook_s || {}).reduce((a, s) => a + s, 0) * 1e6

function proposeKnob(name, spec, value) {
  ask.value = {
    title: `Set ${name}`,
    command: "set_knob",
    body: { name, value },
    changes: [{ label: name, from: spec.value, to: value }],
    note: spec.kind === "policy" ? `Every job admitted from now on goes through ${value}.` : null,
  }
}

function proposePolicy(p, event) {
  const value = event.target.value
  event.target.value = ""
  if (!value) return
  ask.value = {
    title: `Swap ${liveLane.value.name}'s ${p.role}`,
    command: "set_lane_policy",
    body: { lane: liveLane.value.name, policy: p.role, value },
    changes: [{ label: `${liveLane.value.name} ${p.role}`, from: p.current, to: value }],
    note:
      p.role === "former"
        ? "Set between cycles; every item queued on the lane is re-bucketed under the new former. A policy the lane refuses leaves the old one set."
        : "Set between cycles; the new selector picks the unit for the next batches sent. A policy the lane refuses leaves the old one set.",
  }
}

function proposeDetach() {
  const u = liveUnit.value
  ask.value = {
    title: `Detach ${u.name}`,
    command: "detach",
    body: { unit: u.name, drain: true },
    changes: [{ label: "state", from: u.state, to: "detached" }],
    note: `The lane sends ${u.name} nothing new; the ${u.inflight} items it holds finish first (drain). Attaching it again needs the machine's code.`,
    confirm: "Detach",
    danger: true,
  }
}

// What disabling a plugin this page itself reads through costs the page
const SELF = {
  inspector: "Disabling unloads the inspector: its server closes, so this page loses the machine and cannot enable it again (the machine's own code can).",
  telemetry: "Disabling unloads the telemetry plugin: its writer closes its files, and this page's charts get nothing new.",
}

function proposePlugin(p) {
  const off = "A disabled plugin is unloaded: its hooks, contributions and event delivery stop until it is enabled again."
  ask.value = {
    title: `${p.enabled ? "Disable" : "Enable"} ${p.name}`,
    command: p.enabled ? "plugin_disable" : "plugin_enable",
    body: { name: p.name },
    changes: [{ label: p.name, from: p.enabled ? "enabled" : "disabled", to: p.enabled ? "disabled" : "enabled" }],
    note: p.enabled ? [off, SELF[p.name]].filter(Boolean).join(" ") : null,
    danger: p.enabled,
  }
}

const head = computed(() => {
  switch (kind.value) {
    case "dispatcher":
      return { icon: "i-carbon-chip", title: "Dispatcher", chip: `${run.info?.machine?.core ?? ""} core`, note: "One thread: fetch, issue, pump, retire, admit; inline nodes run on it." }
    case "pool":
      return { icon: "i-carbon-layers", title: "Job pool", note: "Resident jobs by state at each sample." }
    case "jobs":
      return { icon: "i-carbon-data-base", title: "Jobs", note: "Admitted from the source, ended into the sink." }
    case "lane":
      return {
        icon: "i-carbon-flow-stream",
        title: name.value,
        chip: `${lane.value?.kind ?? ""} lane`,
        note: lane.value?.stream ? "Paces only: the engine streams and batches by itself; fill is requests in flight over the units' capacity." : null,
      }
    case "unit":
      return { icon: "i-carbon-chip", title: name.value, chip: unit.value?.lane ? `lane ${unit.value.lane}` : null, note: "Fill: items the lane sent and the unit has not answered, over those plus its free capacity." }
  }
  return { icon: "", title: run.part }
})

const tiles = computed(() => {
  const m = props.model
  switch (kind.value) {
    case "dispatcher":
      return [
        { label: "Busy", value: pct(m.dispatcher.busy, 0) },
        { label: "Cycles / s", value: rate(m.dispatcher.cycles) },
        { label: "Calls / s", value: rate(m.dispatcher.calls) },
      ]
    case "pool":
      return [
        { label: "Resident mean", value: num(m.pool.mean, 1) },
        { label: "Peak", value: count(m.pool.peak) },
        { label: "Last", value: count(m.pool.last) },
      ]
    case "jobs":
      return [
        { label: "Admitted / s", value: rate(m.jobs.admittedRate) },
        { label: "Done / s", value: rate(m.jobs.doneRate) },
        { label: "Failed", value: count(m.jobs.failed), tone: m.jobs.failed ? "text-coral-shadow dark:text-coral-light" : "" },
      ]
  }
  const l = lane.value
  if (l)
    return [
      { label: "Items / s", value: rate(l.items) },
      { label: "Fill", value: pct(l.fill, 0) },
      { label: "Batch mean", value: num(l.batch, 1) },
      { label: "Queue p90", value: us(l.queueP90) },
      { label: "Service p90", value: us(l.serviceP90) },
      { label: "Failed", value: l.errors ? count(l.errors) : "—", tone: l.errors ? "text-coral-shadow dark:text-coral-light" : "" },
    ]
  const u = unit.value
  if (u)
    return [
      { label: "Items / s", value: rate(u.items) },
      { label: "Fill", value: pct(u.fill, 0) },
      { label: "Busy", value: pct(u.busy, 0) },
      { label: "Capacity", value: count(u.capacity) },
      { label: "Service p90", value: us(u.serviceP90) },
      { label: "Failed", value: u.errors ? count(u.errors) : "—", tone: u.errors ? "text-coral-shadow dark:text-coral-light" : "" },
    ]
  return []
})

const charts = computed(() => {
  void run.theme
  const out = []
  const add = (title, s, list, format, extra = {}) => {
    const sl = list.filter(Boolean)
    if (s && sl.length) out.push({ title, x: s.t, series: sl, format, ...extra })
  }
  switch (kind.value) {
    case "dispatcher": {
      const busy = perSecond(S("dispatcher/busy_s"))
      add("Busy", busy, [one("busy", busy, seriesColor(0), { fill: true })], (v) => pct(v, 0))
      const calls = perSecond(S("dispatcher/calls"))
      add("Calls per second", calls, [one("calls/s", calls, seriesColor(1), { fill: true })], (v) => rate(v))
      break
    }
    case "pool": {
      const r = S("jobs/resident")
      add("Resident jobs", r, [one("resident", r, seriesColor(0), { fill: true })], (v) => count(v), { integer: true })
      break
    }
    case "jobs": {
      const d = tp.value
      if (d?.done.t.length) {
        const at = new Map(d.adm.t.map((t, i) => [t, d.adm.v[i]]))
        out.push({
          title: "Jobs per second",
          x: d.done.t,
          series: [
            { label: "Admitted", values: d.done.t.map((t) => at.get(t) ?? 0), color: seriesColor(0) },
            { label: "Done", values: d.done.v, color: seriesColor(1), fill: true },
          ],
          format: (v) => rate(v),
        })
      }
      break
    }
  }
  if (lane.value) {
    const l = lane.value
    const q = l.queue.series
    const f = l.inflight.series
    add("Queue and in flight", q, [one("Queued", q, phase("queue"), { fill: true }), f && one("In flight", { v: f.v.slice(0, q?.v.length) }, phase("service"))], (v) => count(v), { integer: true })
    if ((l.engineQueue.peak ?? 0) > 0) {
      // Engines that queue inside themselves (a serving engine's scheduler) report it
      const w = l.engineQueue.series
      const running = sumOf(l.units.map((u) => u.running.series))
      add("Inside the engines", w, [one("Waiting", w, phase("queue"), { fill: true }), running && one("Running", { v: running.v.slice(0, w?.v.length) }, phase("service"))], (v) => count(v), { integer: true })
    }
  }
  if (unit.value) {
    const u = unit.value
    const f = u.fillSeries
    add("Fill", f, [one("fill", f, phase("service"), { fill: true })], (v) => pct(v, 0))
    const w = u.waiting.series
    if (w && (u.waiting.peak ?? 0) > 0) add("Running and waiting in the engine", w, [one("Waiting", w, phase("queue"), { fill: true }), one("Running", u.running.series, phase("service"))], (v) => count(v), { integer: true })
    const h = S(`unit/${u.name}/held`)
    add("Held items (sent, not answered)", h, [one("held", h, phase("queue"), { fill: true })], (v) => count(v), { integer: true })
  }
  return out
})
</script>
