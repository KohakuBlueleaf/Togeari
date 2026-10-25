<template>
  <div class="kt-v2-chrome shrink-0 flex gap-1" :class="compact ? 'flex-col px-2 pt-1.5' : 'h-11 items-end pl-3 pr-3'">
    <div class="flex items-end gap-1 min-w-0" :class="compact ? 'order-2 overflow-x-auto' : ''">
      <button v-if="compact" class="btn-icon self-center shrink-0 mr-1" title="Runs" @click="emit('menu')"><span class="i-carbon-menu" /></button>
      <button
        v-for="[id, label, icon] in VIEWS"
        :key="id"
        class="kt-v2-tab h-9 flex items-center gap-1.5 text-[13px] shrink-0"
        :class="[run.view === id ? 'kt-v2-tab--active font-semibold text-warm-800 dark:text-warm-50' : 'text-warm-600 dark:text-warm-400', compact ? 'px-3' : 'px-3.5']"
        :title="label"
        @click="run.view = id"
      >
        <span :class="icon" class="text-[14px]" /><span v-if="!compact || run.view === id">{{ label }}</span>
      </button>
    </div>

    <div class="flex items-center gap-2" :class="compact ? 'order-1 pb-1' : 'ml-auto self-center'">
      <span v-if="compact" class="text-[13px] font-semibold truncate min-w-0">{{ current?.name }}</span>
      <span v-if="live.state === 'live'" class="chip-sage shrink-0" :title="live.error || 'the machine answers its inspector'"><span class="i-carbon-dot-mark text-[10px] animate-pulse" />live</span>
      <span v-else-if="live.state === 'unreachable'" class="chip-amber shrink-0" title="the run has no stop time and its inspector does not answer: killed, or run without one"><span class="i-carbon-warning-alt-filled text-[10px]" />unreachable</span>
      <span v-if="run.range" class="num text-[11px] text-warm-500 hidden lg:inline">
        {{ clock(run.range[0]) }} – {{ clock(run.range[1]) }} <span class="text-warm-400">({{ span(run.seconds) }})</span>
      </span>
      <div class="flex items-center gap-0.5 p-0.5 rounded-lg bg-warm-200/50 dark:bg-warm-800/60 shrink-0" :class="compact ? 'ml-auto' : ''">
        <button
          v-for="[label, seconds] in PRESETS"
          :key="label"
          class="seg-btn"
          :class="!run.brushed && run.preset === seconds ? 'seg-btn-on' : ''"
          @click="run.setPreset(seconds)"
        >
          {{ label }}
        </button>
        <button v-if="run.brushed" class="seg-btn seg-btn-on flex items-center gap-1" title="Back to the preset range (double-click a chart: back one zoom)" @click="run.unbrush()">
          <span class="i-carbon-zoom-area text-[12px]" /><span v-if="!compact">brushed</span><span class="i-carbon-close text-[12px]" />
        </button>
      </div>
      <button class="btn-icon shrink-0" title="Refresh" @click="run.refresh()"><span class="i-carbon-renew" /></button>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue"

import { useCompact } from "@/composables/useCompact"
import { useLiveStore } from "@/stores/live"
import { PRESETS, useRunStore, VIEWS } from "@/stores/run"
import { clock, span } from "@/utils/format"

const emit = defineEmits(["menu"])
const run = useRunStore()
const live = useLiveStore()
const compact = useCompact()
const current = computed(() => run.runs.find((r) => r.id === run.runId))
</script>
