<template>
  <svg :viewBox="`0 0 ${width} ${height}`" preserveAspectRatio="none" :style="{ width: `${width}px`, height: `${height}px` }" class="block max-w-full overflow-visible" :aria-label="label">
    <path v-if="area" :d="area" :fill="`${stroke}22`" />
    <path v-if="line" :d="line" fill="none" :stroke="stroke" stroke-width="1.5" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
  </svg>
</template>

<script setup>
import { computed } from "vue"

import { useThemeStore } from "@/stores/theme"
import { series } from "@/utils/palette"

/**
 * A sparkline of `values` (nulls skipped) scaled to its own range, shrinking to its container.
 * More values than two per pixel column are drawn as each column's least and greatest value.
 */
const props = defineProps({
  values: { type: Array, default: () => [] },
  width: { type: Number, default: 96 },
  height: { type: Number, default: 22 },
  color: { type: String, default: null },
  label: { type: String, default: "" },
})

const theme = useThemeStore()
const stroke = computed(() => (void theme.dark, props.color || series(0)))
const points = computed(() => {
  const vs = props.values
  let hi = -Infinity
  let lo = 0
  let ok = 0
  for (const v of vs) {
    if (!Number.isFinite(v)) continue
    if (v > hi) hi = v
    if (v < lo) lo = v
    ok++
  }
  if (ok < 2) return []
  const span = hi - lo || 1
  const n = vs.length - 1 || 1
  const x = (i) => (i / n) * props.width
  const y = (v) => props.height - 1 - ((v - lo) / span) * (props.height - 3)
  const cols = Math.max(1, Math.round(props.width))
  const out = []
  if (vs.length <= cols * 2) {
    vs.forEach((v, i) => Number.isFinite(v) && out.push([x(i), y(v)]))
    return out
  }
  // Per pixel column its least and greatest value, in the order they come
  let col = -1
  let a = -1
  let b = -1
  const flush = () => {
    if (a < 0) return
    for (const i of a === b ? [a] : a < b ? [a, b] : [b, a]) out.push([x(i), y(vs[i])])
  }
  vs.forEach((v, i) => {
    if (!Number.isFinite(v)) return
    const c = Math.floor((i / n) * (cols - 1))
    if (c !== col) {
      flush()
      col = c
      a = b = i
    } else {
      if (v < vs[a]) a = i
      if (v > vs[b]) b = i
    }
  })
  flush()
  return out
})
const line = computed(() => points.value.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(""))
const area = computed(() => {
  const p = points.value
  if (!p.length) return ""
  return `${line.value}L${p[p.length - 1][0].toFixed(1)},${props.height}L${p[0][0].toFixed(1)},${props.height}Z`
})
</script>
