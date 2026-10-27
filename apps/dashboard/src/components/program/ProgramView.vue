<template>
  <div class="h-full flex min-h-0" :class="compact ? 'flex-col' : ''">
    <div class="flex-1 min-w-0 flex flex-col" :class="compact ? 'min-h-[60vh]' : ''">
      <!-- Graph toolbar: program, view, what heat shows -->
      <div class="px-4 flex items-center gap-3 kt-v2-line border-b shrink-0" :class="compact ? 'flex-wrap gap-y-2 py-2' : 'h-11'">
        <select v-if="programs.length > 1" v-model="programStart" class="text-xs bg-transparent kt-v2-line border rounded-md h-7 px-2">
          <option v-for="p in programs" :key="p.start" :value="p.start">{{ p.entries.join(", ") || p.name }}</option>
        </select>
        <span v-else class="text-[13px] font-semibold">{{ program?.entries?.join(", ") || program?.name }}</span>
        <span class="num text-[10px] text-warm-400">{{ program?.hash?.slice(0, 10) }}</span>
        <Segmented v-if="sourceDoc" v-model="shape" :options="SHAPE_OPTIONS" />
        <Segmented v-model="prefs.graphView" :options="GRAPH_VIEW_OPTIONS" />
        <!-- The opened @defs: the entry, then each call double-clicked into -->
        <nav v-if="framed" class="flex items-center gap-1 text-xs min-w-0" title="Double-click a call to open its @def">
          <template v-for="(f, i) in frames" :key="i">
            <span v-if="i" class="i-carbon-chevron-right text-warm-400 shrink-0" />
            <button class="kt-row rounded px-1.5 py-0.5 truncate" :class="i === frames.length - 1 ? 'font-semibold' : 'text-warm-500'" @click="frames.splice(i + 1)">@{{ f.graph }}</button>
          </template>
        </nav>
        <div class="ml-auto flex items-center gap-2 text-[11px] text-warm-500">
          <span>Heat</span>
          <Segmented v-model="heatBy" :options="HEAT_OPTIONS" />
          <WaitToggle />
        </div>
      </div>
      <div class="flex-1 min-h-0 relative">
        <KirGraph v-if="doc" :key="`${programStart}-${style}-${shape}-${frames.length}`" :doc="doc" :graph="graphName" :view="view" :overlay="overlay" :selected="selection.node" :minimap="false" class="absolute inset-0" @select="onSelect" @open="onOpen">
          <div class="absolute left-3 bottom-3 kt-v2-float px-2.5 py-1.5 text-[11px] flex items-center gap-3">
            <span class="text-warm-500">{{ HEAT_OPTIONS.find((o) => o[0] === heatBy)[1] }}</span>
            <span class="flex items-center gap-1"><span class="w-16 h-2 rounded-sm kg-heat-scale" /><span class="text-warm-500">low → high</span></span>
            <span class="flex items-center gap-1"><span class="i-carbon-warning-alt-filled" :style="{ color: status('warn') }" />failed attempts</span>
            <span class="flex items-center gap-1"><span class="i-carbon-error-filled" :style="{ color: status('bad') }" />faults</span>
          </div>
        </KirGraph>
        <div v-else-if="docError" class="p-6 text-xs chip-coral">{{ docError }}</div>
      </div>
    </div>

    <!-- The selected statement -->
    <SidePane name="program" label="Statements" icon="i-carbon-list" :initial="380">
      <StatementPanel v-if="selectedGroup" :group="selectedGroup" :rows="selectedRows" />
      <div v-else class="p-5 flex flex-col gap-3">
        <p class="section-title">Statements</p>
        <p class="text-xs text-warm-500">Pick an element on the graph, or a row below, to see its calls, phases and latency over time.</p>
        <button v-for="g in hottest" :key="g.path" class="text-left kt-v2-card px-3 py-2 kt-row" @click="selection.node = g.path">
          <div class="flex items-center gap-2 text-xs min-w-0">
            <span class="font-medium shrink-0">{{ g.node }}</span>
            <span class="num text-[10px] text-warm-400 truncate min-w-0" :title="g.path">{{ g.path }}</span>
            <span class="ml-auto num text-warm-500 shrink-0">{{ pct(g.time / totalTime, 0) }}</span>
          </div>
          <div class="mt-1.5"><PhaseBar :wait="prefs.withWait ? g.wait?.mean : 0" :queue="g.queue?.mean" :service="g.service?.mean" /></div>
        </button>
      </div>
    </SidePane>
  </div>
</template>

<script setup>
import { KirGraph, viewData } from "@kohaku/kir-graph"
import { computed, ref, watch } from "vue"

import PhaseBar from "@/components/charts/PhaseBar.vue"
import StatementPanel from "@/components/program/StatementPanel.vue"
import SidePane from "@/components/shell/SidePane.vue"
import Segmented from "@/components/ui/Segmented.vue"
import WaitToggle from "@/components/ui/WaitToggle.vue"
import { useCompact } from "@/composables/useCompact"
import { useLoad } from "@/composables/useLoad"
import { usePrograms } from "@/composables/usePrograms"
import { usePrefsStore } from "@/stores/prefs"
import { useSelectionStore } from "@/stores/selection"
import { api } from "@/utils/api"
import { count, pct, rate, us } from "@/utils/format"
import { status } from "@/utils/palette"
import { byPath, frameKey, statementRows } from "@/utils/statements"

// KIR's three views; each draws one data style (viewData): the flowchart and the blocks the
// variables style, the node view the wires style
const GRAPH_VIEW_OPTIONS = [
  ["flowchart", "Flowchart"],
  ["nodes", "Node"],
  ["blocks", "Block"],
]
// As written: one graph per @def, a call opened by double-click; inlined: the program as it
// runs, every call inlined into main
const SHAPE_OPTIONS = [
  ["source", "As written"],
  ["lowered", "Inlined"],
]
const HEAT_OPTIONS = [
  ["time", "Total time"],
  ["p90", "Service p90"],
  ["calls", "Calls"],
]

const prefs = usePrefsStore()
const selection = useSelectionStore()
const compact = useCompact()
const { programs, index } = usePrograms()
const programStart = ref(null)
const view = computed(() => prefs.graphView)
const style = computed(() => viewData(view.value))
const heatBy = ref("time")
const shape = ref("source")

watch(programs, (ps) => {
  if (ps.length && !ps.some((p) => p.start === programStart.value)) programStart.value = ps[0].start
})
const program = computed(() => programs.value.find((p) => p.start === programStart.value))
const usable = (g) => (g && !g.error ? g : null)
const sourceDoc = computed(() => usable(program.value?.source_graphs?.[style.value]))
// Frames apply to the source graphs (a program recorded without them shows the inlined one)
const framed = computed(() => shape.value === "source" && !!sourceDoc.value)
const doc = computed(() => (framed.value ? sourceDoc.value : usable(program.value?.graphs?.[style.value])))
const docError = computed(() => {
  const g = framed.value ? program.value?.source_graphs?.[style.value] : program.value?.graphs?.[style.value]
  return g?.error || null
})
const names = computed(() => new Set((doc.value?.graphs || []).map((g) => g.name)))
// The entry @def when the document has it (Togeari runs an entry, never main)
const entryName = computed(() => {
  const entry = program.value?.entry
  return names.value.has(entry) ? entry : names.value.has("main") ? "main" : [...names.value][0] || "main"
})

// The opened @defs: the entry, then each opened call (its node id appended to the prefix)
const frames = ref([])
watch([programStart, framed, entryName], () => (frames.value = [{ graph: entryName.value, prefix: [] }]), { immediate: true })
const frame = computed(() => frames.value[frames.value.length - 1])
const graphName = computed(() => (framed.value ? frame.value.graph : entryName.value))

function onOpen(s) {
  const n = s?.type === "node" ? s.node : null
  if (!framed.value || n?.kind !== "call" || !names.value.has(n.name)) return
  frames.value.push({ graph: n.name, prefix: [...frame.value.prefix, n.origin?.node_id ?? s.key] })
  selection.node = null
}

const { data: table } = useLoad(
  (r) => api.table(r.runId, "node", r.t0, r.t1),
  () => null,
  [],
)
// Each statement under the node it shows on: its lowered path on the inlined graph, its
// frame node (frameKey) on a source graph, where a call holds every statement inlined from it
const rows = computed(() => {
  const own = statementRows(table.value, index.value).filter((r) => r.program === programStart.value)
  if (!framed.value) return own
  const origins = program.value.origins || {}
  const prefix = frame.value.prefix
  return own.map((r) => ({ ...r, path: frameKey(origins[r.path], prefix) })).filter((r) => r.path)
})
// A frame node's label: its call or kind on the shown graph
const labels = computed(() => {
  const g = (doc.value?.graphs || []).find((x) => x.name === graphName.value)
  return new Map((g?.nodes || []).map((n) => [n.path ?? n.id, n.name || n.kind]))
})
const groups = computed(() => {
  const out = byPath(rows.value, prefs.phases)
  if (framed.value) for (const g of out.values()) g.node = labels.value.get(g.path) || g.node
  return out
})
const totalTime = computed(() => [...groups.value.values()].reduce((t, g) => t + g.time, 0) || 1)

// The overlay: heat by the chosen measure (share of the hottest), status from failures,
// badges for calls per second and service p90
const overlay = computed(() => {
  const out = new Map()
  const all = [...groups.value.values()]
  const measure = (g) => (heatBy.value === "time" ? g.time : heatBy.value === "p90" ? (g.service?.p90 ?? 0) : g.calls)
  const top = Math.max(1e-9, ...all.map(measure))
  for (const g of all) {
    const badges = [{ text: `${rate(g.rate)}/s`, tone: "muted", title: "calls per second" }]
    if (g.service) badges.push({ text: us(g.service.p90), tone: "accent", title: "service p90" })
    if (g.errors) badges.push({ text: `${g.errors} failed`, tone: "warn", title: "failed attempts" })
    if (g.faults) badges.push({ text: `${g.faults} faults`, tone: "bad", title: "faults the job saw" })
    out.set(g.path, {
      heat: measure(g) / top,
      status: g.faults ? "error" : g.errors ? "warn" : g.calls ? "ok" : "idle",
      badges,
      note: `${count(g.calls)} calls`,
    })
  }
  return out
})

const hottest = computed(() => [...groups.value.values()].sort((a, b) => b.time - a.time).slice(0, 10))
const selectedGroup = computed(() => (selection.node ? groups.value.get(selection.node) || { path: selection.node, node: selection.node, addrs: [] } : null))
const selectedRows = computed(() => rows.value.filter((r) => r.path === selection.node))

function onSelect(s) {
  selection.node = s?.type === "node" ? s.key : null
}
</script>

<style scoped>
.kg-heat-scale {
  background: linear-gradient(90deg, color-mix(in srgb, var(--kt-color-iolite) 8%, transparent), var(--kt-color-iolite));
}
</style>
