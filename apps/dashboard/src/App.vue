<template>
  <div class="kt-v2 kg-theme h-full flex text-warm-800 dark:text-warm-100">
    <RunRail v-if="!compact" />
    <Teleport to="body">
      <div v-if="compact && drawer" class="kt-v2 kg-theme fixed inset-0 z-50 flex text-warm-800 dark:text-warm-100">
        <div class="absolute inset-0 bg-black/40" @click="drawer = false" />
        <RunRail drawer class="relative h-full w-72 max-w-[85vw] shadow-xl" @close="drawer = false" />
      </div>
    </Teleport>
    <div class="flex-1 flex flex-col min-w-0">
      <TabStrip @menu="drawer = true" />
      <main class="kt-v2-canvas flex-1 min-h-0 overflow-auto">
        <div v-if="run.error" class="m-6 chip-coral !text-xs !py-1.5 !px-2.5"><span class="i-carbon-error-filled" />{{ run.error }}</div>
        <div v-else-if="!run.runId" class="h-full flex flex-col items-center justify-center gap-2 text-warm-500">
          <span class="i-carbon-data-vis-4 text-3xl text-warm-400" />
          <p class="text-sm">No run selected. Runs are directories with a <code class="text-xs">machine.json</code> under the dashboard's roots.</p>
        </div>
        <component :is="VIEW_COMPONENTS[run.view]" v-else-if="run.info" :key="run.runId" />
      </main>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from "vue"

import RunRail from "@/components/shell/RunRail.vue"
import TabStrip from "@/components/shell/TabStrip.vue"
import { useCompact } from "@/composables/useCompact"
import { useLiveStore } from "@/stores/live"
import { useRunStore } from "@/stores/run"
import JobsView from "@/views/jobs/JobsView.vue"
import MachineView from "@/views/machine/MachineView.vue"
import OverviewView from "@/views/overview/OverviewView.vue"
import ProgramView from "@/views/program/ProgramView.vue"
import TimelineView from "@/views/timeline/TimelineView.vue"

const VIEW_COMPONENTS = {
  overview: OverviewView,
  program: ProgramView,
  machine: MachineView,
  jobs: JobsView,
  timeline: TimelineView,
}

const run = useRunStore()
const compact = useCompact()
// The phone's run drawer
const drawer = ref(false)

// The dev server exposes the store to the screenshot script
if (import.meta.env.DEV) window.__dash = { run, live: useLiveStore() }

// URL parameters pick the start state: ?run=&view=&range= (or t0=&t1=)&theme=&node=
onMounted(async () => {
  const wanted = run.fromUrl()
  await run.loadRuns()
  const pick = run.runs.find((r) => r.id === wanted || r.name === wanted) || run.runs[0]
  if (pick) await run.open(pick.id)
})
</script>
