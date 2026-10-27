<template>
  <div class="kt-chart relative" :style="{ height: `${height}px` }">
    <div ref="host" class="absolute inset-0" />
    <div v-if="empty" class="absolute inset-0 flex items-center justify-center text-xs text-warm-400">{{ emptyText }}</div>
  </div>
</template>

<script setup>
import uPlot from "uplot"
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"

import { useRangeStore } from "@/stores/range"
import { useThemeStore } from "@/stores/theme"
import { clock } from "@/utils/format"
import { chrome } from "@/utils/palette"

const INTEGER_INCRS = Array.from({ length: 13 }, (_, k) => [1, 2, 5].map((m) => m * 10 ** k)).flat()

/**
 * A time series chart (uPlot): one y axis, a crosshair with a floating tooltip, a drag across
 * the plot that sets the dashboard's range, and a double-click that goes back one such zoom.
 *
 * Props:
 * - `x`: unix seconds; `series`: `[{label, values, color, fill?, dash?, band?: [lo, hi]}]`;
 * - `format(v)`: the y value as text (axis and tooltip);
 * - `sync`: a cursor sync key shared by charts that move together;
 * - `stepped`: draw values as steps (counts per bucket) instead of lines;
 * - `integer`: y ticks only at whole numbers (queue lengths, counts);
 * - `marks`: `[{t, label, color}]`, events drawn as dashed vertical rules, listed in the
 *   tooltip when the cursor is within a few pixels of one.
 */
const props = defineProps({
  x: { type: Array, default: () => [] },
  series: { type: Array, default: () => [] },
  format: { type: Function, default: (v) => String(v) },
  height: { type: Number, default: 180 },
  sync: { type: String, default: "togeari" },
  stepped: { type: Boolean, default: false },
  integer: { type: Boolean, default: false },
  yMin: { type: Number, default: 0 },
  emptyText: { type: String, default: "No data in this range" },
  marks: { type: Array, default: () => [] },
})

// How near (CSS px) the cursor must be to a mark for the tooltip to list it
const MARK_HIT = 5

const range = useRangeStore()
const theme = useThemeStore()
const host = ref(null)
const empty = ref(true)
let plot = null
let tip = null
let observer = null
let lines = []
// What the plot was built for: series labels, colors, styles and the theme
let shape = null
// A drag started on this chart (not a selection synced from another)
let dragged = false

const shapeOf = () => JSON.stringify([theme.dark, props.stepped, props.integer, props.series.map((s) => [s.label, s.color, !!s.fill, s.dash, !!s.band])])

/** uPlot's data: x, then per series its band's max and min (when it has one) and its values. */
function columns() {
  const data = [props.x]
  for (const s of props.series) {
    if (s.band) data.push(s.band[1], s.band[0])
    data.push(s.values)
  }
  return data
}

/** New data on the plot already built, or a new plot when its series or theme changed. */
function update() {
  const isEmpty = !props.x.length || !props.series.length
  if (plot && !isEmpty && shape === shapeOf()) {
    plot.setData(columns())
    return
  }
  build()
}

function build() {
  plot?.destroy()
  plot = null
  tip?.remove()
  tip = null
  shape = null
  empty.value = !props.x.length || !props.series.length
  if (empty.value || !host.value) return
  shape = shapeOf()
  const c = chrome()
  const width = host.value.clientWidth || 300
  const fmt = props.format
  const paths = props.stepped ? uPlot.paths.stepped({ align: 1 }) : undefined

  // Bands draw first, under their line; `lines` are the series the tooltip lists
  lines = []
  const series = [{}]
  const bands = []
  for (const s of props.series) {
    if (s.band) {
      series.push({ label: `${s.label} max`, stroke: "transparent", points: { show: false }, paths })
      series.push({ label: `${s.label} min`, stroke: "transparent", points: { show: false }, paths })
      bands.push({ series: [series.length - 2, series.length - 1], fill: `${s.color}22` })
    }
    lines.push([series.length, s.color])
    series.push({
      label: s.label,
      stroke: s.color,
      width: 2,
      fill: s.fill ? `${s.color}1f` : undefined,
      dash: s.dash,
      points: { show: false },
      paths,
      value: (_, v) => fmt(v),
    })
  }

  const axis = {
    stroke: c.axis,
    grid: { stroke: c.grid, width: 1 },
    ticks: { stroke: c.tick, width: 1, size: 4 },
    font: "11px Inter, system-ui, sans-serif",
  }
  plot = new uPlot(
    {
      width,
      height: props.height,
      padding: [8, 8, 0, 0],
      cursor: {
        sync: { key: props.sync, setSeries: false },
        drag: { x: true, y: false, setScale: false },
        points: { size: 7, width: 2 },
      },
      legend: { show: false },
      select: { show: true },
      scales: { x: { time: true }, y: { range: (u, lo, hi) => [Math.min(props.yMin, lo), hi > 0 ? hi * 1.08 : 1] } },
      axes: [
        { ...axis, values: (u, ticks) => ticks.map((t) => clock(t)) },
        { ...axis, size: 64, values: (u, ticks) => ticks.map(fmt), ...(props.integer ? { incrs: INTEGER_INCRS } : {}) },
      ],
      series,
      bands,
      hooks: {
        setSelect: [
          (u) => {
            // Synced charts see the same selection: only the one dragged on sets the range
            const mine = dragged
            dragged = false
            if (u.select.width < 4) return
            const a = u.posToVal(u.select.left, "x")
            const b = u.posToVal(u.select.left + u.select.width, "x")
            u.setSelect({ width: 0, height: 0 }, false)
            if (mine) range.brush(a, b)
          },
        ],
        setCursor: [(u) => showTip(u)],
        draw: [(u) => drawMarks(u)],
      },
    },
    columns(),
    host.value,
  )
  plot.over.addEventListener("mousedown", () => (dragged = true))
  plot.over.addEventListener("dblclick", () => range.zoomOut())
  tip = document.createElement("div")
  tip.className = "kt-tip kt-v2-float"
  tip.style.display = "none"
  host.value.appendChild(tip)
}

function drawMarks(u) {
  const { left, top, width, height } = u.bbox
  const ctx = u.ctx
  ctx.save()
  ctx.lineWidth = 1.5 * devicePixelRatio
  ctx.setLineDash([4 * devicePixelRatio, 3 * devicePixelRatio])
  for (const m of props.marks) {
    const x = Math.round(u.valToPos(m.t, "x", true))
    if (x < left || x > left + width) continue
    ctx.strokeStyle = m.color
    ctx.beginPath()
    ctx.moveTo(x, top)
    ctx.lineTo(x, top + height)
    ctx.stroke()
  }
  ctx.restore()
}

function showTip(u) {
  const i = u.cursor.idx
  if (!tip) return
  if (i === null || i === undefined || u.cursor.left < 0) {
    tip.style.display = "none"
    return
  }
  const near = props.marks.filter((m) => Math.abs(u.valToPos(m.t, "x") - u.cursor.left) <= MARK_HIT)
  const rows = near.map((m) => `<div class="kt-tip__row"><span class="kt-tip__swatch" style="background:${m.color}"></span>${clock(m.t)} ${escape(m.label)}</div>`)
  for (const [k, color] of lines) {
    const v = u.data[k][i]
    rows.push(`<div class="kt-tip__row"><span class="kt-tip__swatch" style="background:${color}"></span>${u.series[k].label}<span class="kt-tip__value">${v === null || v === undefined ? "—" : props.format(v)}</span></div>`)
  }
  tip.innerHTML = `<div style="opacity:.7;margin-bottom:2px">${clock(u.data[0][i], true)}</div>${rows.join("")}`
  tip.style.display = "block"
  const left = u.cursor.left + u.over.offsetLeft
  const flip = left + tip.offsetWidth + 16 > host.value.clientWidth
  tip.style.left = `${flip ? left - tip.offsetWidth - 12 : left + 12}px`
  tip.style.top = `${Math.max(0, u.cursor.top - 8)}px`
}

onMounted(() => {
  build()
  observer = new ResizeObserver(() => {
    if (plot && host.value) plot.setSize({ width: host.value.clientWidth, height: props.height })
  })
  observer.observe(host.value)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  plot?.destroy()
})
watch(
  () => [props.x, props.series, theme.dark],
  () => nextTick(update),
  { deep: false },
)
watch(
  () => props.marks,
  () => plot?.redraw(false, false),
)

function escape(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c])
}
</script>
