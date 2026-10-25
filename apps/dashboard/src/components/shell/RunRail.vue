<template>
  <!-- Collapsed: an icon strip, one dot per run -->
  <aside v-if="strip" class="kt-v2-rail kt-v2-edge border-r flex flex-col items-center min-h-0 shrink-0 py-2 gap-1" :style="{ width: `${STRIP}px` }">
    <button class="w-8 h-8 flex items-center justify-center rounded-md hover:bg-[var(--v2-tab-hover)]" title="Show the runs" @click="pane.toggle()">
      <span class="i-carbon-flow-data text-amber text-lg" />
    </button>
    <button class="btn-icon" title="Show the runs" @click="pane.toggle()"><span class="i-carbon-side-panel-open" /></button>
    <div class="w-6 my-1 kt-v2-line border-t" />
    <nav class="flex-1 min-h-0 overflow-auto flex flex-col items-center gap-1">
      <button
        v-for="r in run.runs"
        :key="r.id"
        class="w-8 h-8 shrink-0 rounded-md flex items-center justify-center text-[11px] font-semibold uppercase"
        :class="r.id === run.runId ? 'bg-[var(--v2-canvas)] shadow-sm text-warm-800 dark:text-warm-100' : 'text-warm-500 hover:bg-[var(--v2-tab-hover)]'"
        :title="`${r.name} · ${state(r)}`"
        @click="run.open(r.id)"
      >
        <span class="relative">
          {{ r.name.slice(0, 2) }}
          <span v-if="state(r) !== 'stopped'" class="absolute -top-0.5 -right-1.5 w-1.5 h-1.5 rounded-full" :class="DOT[state(r)]" />
        </span>
      </button>
    </nav>
    <button class="btn-icon" :title="run.theme === 'dark' ? 'Light theme' : 'Dark theme'" @click="run.setTheme(run.theme === 'dark' ? 'light' : 'dark')">
      <span :class="run.theme === 'dark' ? 'i-carbon-sun' : 'i-carbon-moon'" />
    </button>
  </aside>

  <aside v-else class="kt-v2-rail kt-v2-edge border-r flex flex-col min-h-0 relative shrink-0" :style="drawer ? null : { width: `${pane.width}px` }">
    <div class="h-11 px-4 flex items-center gap-2 shrink-0">
      <span class="i-carbon-flow-data text-amber text-lg" />
      <span class="text-[15px] tracking-tight"><b class="font-bold text-amber">Toge</b><span class="font-light text-iolite dark:text-iolite-light">ari</span></span>
      <span class="ml-1 text-[11px] text-warm-500">dashboard</span>
      <button v-if="drawer" class="btn-icon ml-auto" title="Close" @click="$emit('close')"><span class="i-carbon-close" /></button>
      <button v-else class="btn-icon ml-auto !w-6 !h-6" title="Hide the runs" @click="pane.toggle()"><span class="i-carbon-side-panel-close text-[14px]" /></button>
    </div>

    <div class="px-4 pt-3 pb-1.5 flex items-center">
      <span class="section-title">Runs</span>
      <span class="ml-1.5 num text-[10px] text-warm-400">{{ run.runs.length }}</span>
      <button class="btn-icon ml-auto !w-6 !h-6" title="Look for runs again" @click="run.loadRuns()"><span class="i-carbon-renew text-[13px]" /></button>
    </div>
    <nav class="flex-1 min-h-0 overflow-auto px-2 pb-2 flex flex-col gap-0.5">
      <button
        v-for="r in run.runs"
        :key="r.id"
        class="text-left rounded-lg px-2.5 py-2 transition-colors"
        :class="r.id === run.runId ? 'bg-[var(--v2-canvas)] shadow-sm' : 'hover:bg-[var(--v2-tab-hover)]'"
        @click="pickRun(r.id)"
      >
        <div class="flex items-center gap-1.5">
          <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="DOT[state(r)]" :title="state(r)" />
          <span class="text-[13px] font-medium truncate">{{ r.name }}</span>
          <span class="ml-auto text-[10px]" :class="state(r) === 'unreachable' ? 'text-amber-shadow dark:text-amber-light' : 'text-warm-500'">{{ state(r) }}</span>
        </div>
        <div class="mt-0.5 pl-3 flex items-center gap-2 text-[11px] text-warm-500">
          <span class="num">{{ clock(r.started, true) }}</span>
          <span v-if="r.stopped" class="num">· {{ span(r.stopped - r.started) }}</span>
        </div>
        <div class="mt-0.5 pl-3 flex items-center gap-1 text-[10px] text-warm-400">
          <span>{{ r.core }} core</span><span>·</span><span>{{ r.ring?.replace("Ring", " ring") }}</span>
        </div>
      </button>
      <p v-if="!run.runs.length" class="px-2.5 py-3 text-xs text-warm-500">No runs found.</p>
    </nav>

    <div class="kt-v2-edge border-t px-3 h-11 flex items-center gap-1 shrink-0">
      <span class="text-[11px] text-warm-500 truncate" :title="current?.path">{{ current?.path }}</span>
      <button class="btn-icon ml-auto shrink-0" :title="run.theme === 'dark' ? 'Light theme' : 'Dark theme'" @click="run.setTheme(run.theme === 'dark' ? 'light' : 'dark')">
        <span :class="run.theme === 'dark' ? 'i-carbon-sun' : 'i-carbon-moon'" />
      </button>
    </div>

    <div
      v-if="!drawer"
      class="absolute top-0 -right-[3px] h-full w-1.5 cursor-col-resize z-10 transition-colors hover:bg-iolite/30"
      :class="pane.dragging ? 'bg-iolite/50' : ''"
      title="Drag to resize · double-click to reset"
      @pointerdown="pane.startDrag"
      @dblclick="pane.reset()"
    />
  </aside>
</template>

<script setup>
import { computed } from "vue"

import { usePane } from "@/composables/usePane"
import { useLiveStore } from "@/stores/live"
import { useRunStore } from "@/stores/run"
import { clock, span } from "@/utils/format"

/** The run list. `drawer`: the phone drawer's copy, full width of its container, never a strip. */
const props = defineProps({ drawer: { type: Boolean, default: false } })
const emit = defineEmits(["close"])

const STRIP = 48
const DOT = { live: "bg-sage animate-pulse", unreachable: "bg-amber", stopped: "bg-warm-400" }
const run = useRunStore()
const live = useLiveStore()
// The open run's state is the live store's latest probe; the others', the listing's
const state = (r) => (r.id === run.runId && live.state ? live.state : r.state || (r.stopped ? "stopped" : "unreachable"))
const pane = usePane("rail", { initial: 240, min: 180, max: 420, edge: "right" })
const strip = computed(() => !props.drawer && pane.collapsed)
const current = computed(() => run.runs.find((r) => r.id === run.runId))

function pickRun(id) {
  run.open(id)
  if (props.drawer) emit("close")
}
</script>
