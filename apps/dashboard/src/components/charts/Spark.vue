<template>
  <svg :viewBox="`0 0 ${width} ${height}`" preserveAspectRatio="none" :style="{ width: `${width}px`, height: `${height}px` }" class="block max-w-full overflow-visible" :aria-label="label">
    <path v-if="area" :d="area" :fill="`${stroke}22`" />
    <path v-if="line" :d="line" fill="none" :stroke="stroke" stroke-width="1.5" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
  </svg>
</template>

<script setup>
import { computed } from "vue"

import { useRunStore } from "@/stores/run"
import { series } from "@/utils/palette"

/** A sparkline of `values` (nulls skipped) scaled to its own range, shrinking to its container. */
const props = defineProps({
  values: { type: Array, default: () => [] },
  width: { type: Number, default: 96 },
  height: { type: Number, default: 22 },
  color: { type: String, default: null },
  label: { type: String, default: "" },
})

const run = useRunStore()
const stroke = computed(() => (void run.theme, props.color || series(0)))
const points = computed(() => {
  const vs = props.values
  const ok = vs.map((v, i) => [i, v]).filter(([, v]) => v !== null && v !== undefined && Number.isFinite(v))
  if (ok.length < 2) return []
  const hi = Math.max(...ok.map(([, v]) => v))
  const lo = Math.min(0, ...ok.map(([, v]) => v))
  const span = hi - lo || 1
  const n = vs.length - 1 || 1
  return ok.map(([i, v]) => [(i / n) * props.width, props.height - 1 - ((v - lo) / span) * (props.height - 3)])
})
const line = computed(() => points.value.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(""))
const area = computed(() => {
  const p = points.value
  if (!p.length) return ""
  return `${line.value}L${p[p.length - 1][0].toFixed(1)},${props.height}L${p[0][0].toFixed(1)},${props.height}Z`
})
</script>
