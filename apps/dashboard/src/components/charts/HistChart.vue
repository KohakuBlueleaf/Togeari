<template>
  <div class="relative" @mouseleave="hover = null">
    <svg v-if="bars.length" :viewBox="`0 0 ${W} ${H}`" class="w-full block" :style="{ height: `${H}px` }" preserveAspectRatio="none">
      <line :x1="0" :x2="W" :y1="H - AX" :y2="H - AX" :stroke="c.tick" />
      <rect
        v-for="(b, i) in bars"
        :key="i"
        :x="b.x + 1"
        :y="b.y"
        :width="Math.max(1, b.w - 2)"
        :height="b.h"
        rx="2"
        :fill="color"
        :opacity="hover === null || hover === i ? 1 : 0.45"
        @mouseenter="hover = i"
      />
      <rect v-for="(b, i) in bars" :key="`hit${i}`" :x="b.x" :y="0" :width="b.w" :height="H - AX" fill="transparent" @mouseenter="hover = i" />
      <g v-for="m in marks" :key="m.label">
        <line :x1="m.x" :x2="m.x" :y1="10" :y2="H - AX" :stroke="c.ink" stroke-dasharray="3 3" stroke-width="1" opacity="0.55" />
        <text :x="m.x + 3" y="9" :fill="c.axis" font-size="10" font-family="Inter, sans-serif">{{ m.label }}</text>
      </g>
      <text v-for="t in ticks" :key="t.x" :x="t.x" :y="H - 3" :fill="c.axis" font-size="10" text-anchor="middle" font-family="Inter, sans-serif">{{ t.label }}</text>
    </svg>
    <div v-else class="h-24 flex items-center justify-center text-xs text-warm-400">No values in this range</div>
    <div v-if="hover !== null && bars[hover]" class="kt-tip kt-v2-float" :style="tipStyle">
      <div class="kt-tip__row">{{ bars[hover].range }}</div>
      <div class="kt-tip__row">
        calls<span class="kt-tip__value">{{ count(bars[hover].n) }} ({{ pct(bars[hover].n / total) }})</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue"

import { useRunStore } from "@/stores/run"
import { count, pct, us } from "@/utils/format"
import { chrome, phase } from "@/utils/palette"

/**
 * A log-binned histogram (the telemetry's 64 bins, each 0.42 or 0.58 of an octave wide), drawn
 * from the first to the last bin with values, with the p50 / p90 / p99 marks.
 *
 * `hist`: the API's `{counts, edges, max, p50, p90, p99}` (the last edge null: the observed max
 * stands for it); `format`: a bin edge as text (µs by default).
 */
const props = defineProps({
  hist: { type: Object, default: null },
  format: { type: Function, default: us },
  phase: { type: String, default: "service" },
})

const run = useRunStore()
const W = 600
const H = 120
const AX = 16
const hover = ref(null)
const c = computed(() => (void run.theme, chrome()))
const color = computed(() => (void run.theme, phase(props.phase)))

const range = computed(() => {
  const counts = props.hist?.counts || []
  const first = counts.findIndex((n) => n > 0)
  if (first < 0) return null
  let last = counts.length - 1
  while (last > first && !counts[last]) last--
  return [first, last]
})
const total = computed(() => (props.hist?.counts || []).reduce((a, b) => a + b, 0))

const bars = computed(() => {
  const r = range.value
  if (!r) return []
  const counts = props.hist.counts
  const edges = props.hist.edges
  const n = r[1] - r[0] + 1
  const w = W / n
  const peak = Math.max(...counts.slice(r[0], r[1] + 1))
  return counts.slice(r[0], r[1] + 1).map((v, k) => {
    const i = r[0] + k
    const h = peak ? ((H - AX - 14) * v) / peak : 0
    const upper = edges[i + 1] ?? props.hist.max
    return { x: k * w, w, y: H - AX - h, h, n: v, range: `${props.format(edges[i])} – ${props.format(upper)}` }
  })
})

// Where a value sits: its bin, then linearly inside it
function xOf(v) {
  const r = range.value
  const edges = props.hist.edges
  const n = r[1] - r[0] + 1
  for (let i = r[0]; i <= r[1]; i++) {
    if (v < edges[i + 1] || i === r[1]) {
      const f = Math.min(1, Math.max(0, (v - edges[i]) / (edges[i + 1] - edges[i] || 1)))
      return ((i - r[0] + f) * W) / n
    }
  }
  return W
}

const marks = computed(() => {
  if (!range.value) return []
  return ["p50", "p90", "p99"].filter((p) => props.hist[p] != null).map((p) => ({ label: p, x: xOf(props.hist[p]) }))
})

const ticks = computed(() => {
  const r = range.value
  if (!r) return []
  const n = r[1] - r[0] + 1
  const every = Math.max(1, Math.ceil(n / 6))
  const out = []
  for (let k = 0; k <= n; k += every) out.push({ x: Math.min(W - 14, Math.max(14, (k * W) / n)), label: props.format(props.hist.edges[r[0] + k]) })
  return out
})

const tipStyle = computed(() => {
  const b = bars.value[hover.value]
  const left = (b.x + b.w / 2) / W
  return { left: `calc(${left * 100}% ${left > 0.6 ? "- 11rem" : "+ 8px"})`, top: "8px" }
})
</script>
