<template>
  <div ref="host" class="relative" @mouseleave="hover = null">
    <svg :width="width" :height="height" class="block">
      <!-- Time axis -->
      <g v-for="t in ticks" :key="t.x">
        <line :x1="t.x" :x2="t.x" :y1="AXIS" :y2="height" :stroke="c.grid" />
        <text :x="t.x" :y="12" :fill="c.axis" font-size="10" text-anchor="middle" font-family="Inter, sans-serif">{{ t.label }}</text>
      </g>
      <!-- Rows -->
      <g v-for="(r, i) in fall.rows" :key="i" @mouseenter="hover = i">
        <rect :x="0" :y="y(i) - 2" :width="width" :height="ROW" :fill="hover === i ? c.hover : 'transparent'" />
        <text :x="6" :y="y(i) + 11" :fill="c.ink" font-size="11" font-family="Inter, sans-serif">{{ clip(r.node, 18) }}</text>
        <text :x="label - 8" :y="y(i) + 11" :fill="c.axis" font-size="10" text-anchor="end" font-family="Inter, sans-serif">{{ clip(r.unit || r.lane || "", 14) }}</text>
        <rect v-for="(s, k) in r.segs" :key="k" :x="x(s.a)" :y="y(i) + 2" :width="Math.max(2, x(s.b) - x(s.a) - 1)" :height="ROW - 8" rx="2" :fill="phaseColor(s.phase)" />
        <circle v-if="r.status" :cx="x(r.end) + 6" :cy="y(i) + ROW / 2 - 2" r="3.5" :fill="bad" />
      </g>
      <!-- Faults, parks, resumes -->
      <g v-for="(m, i) in fall.marks" :key="`m${i}`">
        <line :x1="x(m.t)" :x2="x(m.t)" :y1="AXIS" :y2="height" :stroke="m.kind === 'fault' ? bad : c.axis" stroke-dasharray="2 3" />
      </g>
    </svg>
    <div v-if="hover !== null" class="kt-tip kt-v2-float" :style="{ left: `${label + 8}px`, top: `${y(hover) + ROW + 4}px` }">
      <div class="kt-tip__row font-medium">
        {{ row.node }} <span style="opacity: 0.6">@{{ row.addr }}</span>
      </div>
      <div class="kt-tip__row" style="opacity: 0.7">{{ [row.lane, row.unit].filter(Boolean).join(" · ") }}</div>
      <div v-for="s in row.segs" :key="s.phase" class="kt-tip__row">
        <span class="kt-tip__swatch" :style="{ background: phaseColor(s.phase) }" />{{ PHASE_LABEL[s.phase] }}<span class="kt-tip__value">{{ us(s.b - s.a) }}</span>
      </div>
      <div class="kt-tip__row" style="opacity: 0.7">
        from admission<span class="kt-tip__value">{{ us(row.start) }} → {{ us(row.end) }}</span>
      </div>
      <div v-if="row.status" class="kt-tip__row" :style="{ color: bad }">failed (status {{ row.status }})</div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue"

import { useThemeStore } from "@/stores/theme"
import { us } from "@/utils/format"
import { chrome, PHASE_LABEL, phase, status } from "@/utils/palette"

/** A job's statement runs on one time axis from its admission (`utils/waterfall.js`). */
const props = defineProps({ fall: { type: Object, required: true } })

const ROW = 22
const AXIS = 20
const theme = useThemeStore()
const host = ref(null)
const width = ref(800)
const hover = ref(null)
let observer = null

const c = computed(() => ({ ...chrome(), hover: theme.dark ? "rgba(255,255,255,0.04)" : "rgba(36,33,29,0.04)" }))
const bad = computed(() => (void theme.dark, status("bad")))
const phaseColor = (p) => (void theme.dark, phase(p))
const height = computed(() => AXIS + props.fall.rows.length * ROW + 8)
const row = computed(() => props.fall.rows[hover.value])
const total = computed(() => Math.max(1, props.fall.total, ...props.fall.rows.map((r) => r.end)))
// The statement and unit column: 230 px, narrower on a narrow card
const label = computed(() => Math.min(230, Math.round(width.value * 0.45)))
const x = (t) => label.value + (Math.max(0, t) / total.value) * (width.value - label.value - 16)
const y = (i) => AXIS + i * ROW

// One time label per 80 px of plot, at most 6
const ticks = computed(() => {
  const n = Math.max(1, Math.min(6, Math.floor((width.value - label.value - 16) / 80)))
  return Array.from({ length: n + 1 }, (_, k) => ({ x: x((total.value * k) / n), label: us((total.value * k) / n) }))
})
const clip = (s, n) => (s && s.length > n ? `${s.slice(0, n - 1)}…` : s)

onMounted(() => {
  observer = new ResizeObserver(() => (width.value = host.value?.clientWidth || 800))
  observer.observe(host.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>
