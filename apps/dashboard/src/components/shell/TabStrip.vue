<template>
  <div class="kt-v2-chrome shrink-0 flex gap-1" :class="compact ? 'flex-col px-2 pt-1.5' : 'h-11 items-end pl-3 pr-3'">
    <div class="flex items-end gap-1 min-w-0" :class="compact ? 'order-2 overflow-x-auto' : ''">
      <button v-if="compact" class="btn-icon self-center shrink-0 mr-1" title="Runs" @click="emit('menu')"><span class="i-carbon-menu" /></button>
      <template v-if="run.runId">
        <RouterLink v-for="[id, label, icon] in VIEWS" :key="id" :to="viewPath(run.runId, id)" class="kt-v2-tab h-9 flex items-center gap-1.5 text-[13px] shrink-0 no-underline" :class="[view === id ? 'kt-v2-tab--active font-semibold text-warm-800 dark:text-warm-50' : 'text-warm-600 dark:text-warm-400', compact ? 'px-3' : 'px-3.5']" :title="label">
          <span :class="icon" class="text-[14px]" /><span v-if="!compact || view === id">{{ label }}</span>
        </RouterLink>
      </template>
    </div>

    <div class="flex items-center gap-2" :class="compact ? 'order-1 pb-1' : 'ml-auto self-center'">
      <span v-if="compact" class="text-[13px] font-semibold truncate min-w-0">{{ current?.name }}</span>
      <span v-if="live.state === 'live'" class="chip-sage shrink-0" :title="live.error || 'the machine answers its inspector'"><span class="i-carbon-dot-mark text-[10px] animate-pulse" />live</span>
      <span v-else-if="live.state === 'unreachable'" class="chip-amber shrink-0" title="the run has no stop time and its inspector does not answer: killed, or run without one"><span class="i-carbon-warning-alt-filled text-[10px]" />unreachable</span>
      <span v-if="range.range" class="num text-[11px] text-warm-500 hidden lg:inline">
        {{ clock(range.range[0]) }} – {{ clock(range.range[1]) }} <span class="text-warm-400">({{ span(range.seconds) }})</span>
      </span>
      <div class="flex items-center gap-0.5 p-0.5 rounded-lg bg-warm-200/50 dark:bg-warm-800/60 shrink-0" :class="compact ? 'ml-auto' : ''">
        <button v-for="[label, seconds] in PRESETS" :key="label" class="seg-btn" :class="!range.brushed && range.preset === seconds ? 'seg-btn-on' : ''" @click="range.setPreset(seconds)">
          {{ label }}
        </button>
        <button v-if="range.brushed" class="seg-btn seg-btn-on flex items-center gap-1" title="Back to the preset range (double-click a chart: back one zoom)" @click="range.unbrush()"><span class="i-carbon-zoom-area text-[12px]" /><span v-if="!compact">brushed</span><span class="i-carbon-close text-[12px]" /></button>
      </div>
      <label class="flex items-center gap-1 text-[11px] text-warm-500 shrink-0" title="Rate charts' buckets: Auto one per screen pixel, Full every stored row (1 s), or a fixed step">
        <span class="i-carbon-chart-average text-[13px]" />
        <select v-model="prefs.resolution" class="bg-transparent kt-v2-line border rounded-md h-6 px-1 text-[11px]">
          <option v-for="[value, label] in RESOLUTIONS" :key="value" :value="value">{{ label }}</option>
        </select>
      </label>
      <button class="btn-icon shrink-0" title="Refresh" @click="run.refresh()"><span class="i-carbon-renew" /></button>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue"
import { useRoute } from "vue-router"

import { useCompact } from "@/composables/useCompact"
import { useLiveStore } from "@/stores/live"
import { RESOLUTIONS, usePrefsStore } from "@/stores/prefs"
import { PRESETS, useRangeStore } from "@/stores/range"
import { useRunStore } from "@/stores/run"
import { useRunsStore } from "@/stores/runs"
import { clock, span } from "@/utils/format"
import { VIEWS, viewOf, viewPath } from "@/utils/views"

const emit = defineEmits(["menu"])
const route = useRoute()
const run = useRunStore()
const runs = useRunsStore()
const range = useRangeStore()
const prefs = usePrefsStore()
const live = useLiveStore()
const compact = useCompact()
const view = computed(() => viewOf(route))
const current = computed(() => runs.list.find((r) => r.id === run.runId))
</script>
