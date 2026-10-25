<template>
  <div class="h-full flex min-h-0" :class="compact ? 'flex-col' : ''">
    <!-- Kept traces -->
    <SidePane name="jobs" side="left" label="Kept traces" icon="i-carbon-list-boxes" :initial="400" :min="330" :scroll="false">
      <div class="h-11 px-4 flex items-center gap-2 kt-v2-line border-b shrink-0">
        <template v-if="!compact">
          <span class="i-carbon-list-boxes text-warm-500" />
          <span class="card-title">Kept traces</span>
        </template>
        <span class="num text-[11px] text-warm-400">{{ traces?.length ?? 0 }}<template v-if="compact"> kept</template></span>
        <Segmented v-model="reason" :options="REASONS" class="ml-auto" />
      </div>
      <div class="flex-1 overflow-auto">
        <table class="w-full text-xs">
          <thead class="text-warm-400 sticky top-0 bg-[var(--v2-panel)]">
            <tr class="kt-v2-line border-b">
              <th class="text-left font-medium px-4 h-8">Ended</th>
              <th class="text-right font-medium px-2">Job</th>
              <th class="text-left font-medium px-2">Kept</th>
              <th class="text-right font-medium px-4">Wall</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="t in traces || []"
              :key="t.key"
              class="kt-v2-line border-b cursor-pointer kt-row"
              :class="t.key === key ? 'kt-row--on' : ''"
              @click="key = t.key"
            >
              <td class="px-4 h-9 num text-warm-600 dark:text-warm-300">{{ clock(t.t_end) }}</td>
              <td class="px-2 text-right num">{{ t.job }}</td>
              <td class="px-2">
                <span :class="REASON_CHIP[t.reason]">
                  <span v-if="t.reason === 'failed'" class="i-carbon-error-filled text-[10px]" />{{ t.reason }}
                </span>
              </td>
              <td class="px-4 text-right num">{{ us(t.wall_us) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="!traces?.length" class="px-4 py-4 text-xs text-warm-400">No traces kept in this range.</p>
      </div>
    </SidePane>

    <div class="flex-1 min-w-0 overflow-auto flex flex-col gap-4" :class="compact ? 'p-3' : 'p-5'">
      <!-- Resident jobs of the live machine, with what a command can do to each -->
      <SectionCard v-if="live.live" title="Resident jobs" icon="i-carbon-layers" :count="resident.length" :hint="resident.length > SHOWN ? `the first ${SHOWN} by id` : 'live'" flush>
        <template #actions><span class="chip-sage"><span class="i-carbon-dot-mark text-[10px] animate-pulse" />live</span></template>
        <div class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead class="text-warm-400">
              <tr class="kt-v2-line border-b">
                <th class="text-right font-medium px-4 h-8">Job</th>
                <th class="text-left font-medium px-2">Entry</th>
                <template v-if="!compact">
                  <th class="text-left font-medium px-2">Key</th>
                  <th class="text-right font-medium px-2">Strands</th>
                  <th class="text-right font-medium px-2" title="statements fetched and not yet retired">Window</th>
                </template>
                <th class="text-right font-medium px-2" title="statements of the job not yet retired">{{ compact ? "Left" : "Unfinished" }}</th>
                <th class="text-left font-medium px-2">State</th>
                <th class="px-4" />
              </tr>
            </thead>
            <tbody>
              <tr v-for="j in resident.slice(0, SHOWN)" :key="j.id" class="kt-v2-line border-b last:border-b-0 kt-row">
                <td class="px-4 h-9 text-right num">{{ j.id }}</td>
                <td class="px-2 num">{{ j.entry }}</td>
                <template v-if="!compact">
                  <td class="px-2 num text-warm-500 truncate max-w-[10rem]" :title="String(j.key)">{{ j.key }}</td>
                  <td class="px-2 text-right num">{{ j.strands }}</td>
                  <td class="px-2 text-right num">{{ j.window }}</td>
                </template>
                <td class="px-2 text-right num">{{ j.unfinished }}</td>
                <td class="px-2"><span :class="j.paused ? 'chip-amber' : 'chip-warm'">{{ j.paused ? "paused" : "running" }}</span></td>
                <td class="px-4">
                  <div class="flex items-center justify-end gap-0.5">
                    <button class="btn-icon !w-6 !h-6" :title="j.paused ? 'Resume' : 'Pause'" @click="propose(j, j.paused ? 'resume_job' : 'pause_job')">
                      <span :class="j.paused ? 'i-carbon-play' : 'i-carbon-pause'" class="text-[13px]" />
                    </button>
                    <button class="btn-icon !w-6 !h-6" title="Checkpoint" @click="propose(j, 'checkpoint_job')"><span class="i-carbon-save text-[13px]" /></button>
                    <button class="btn-icon !w-6 !h-6 hover:!text-coral" title="Cancel" @click="propose(j, 'cancel_job')"><span class="i-carbon-stop-outline text-[13px]" /></button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="!resident.length" class="px-4 py-4 text-xs text-warm-400">No job resident now.</p>
        </div>
      </SectionCard>

      <!-- The trace -->
      <SectionCard v-if="trace" :title="`Job ${trace.job}`" icon="i-carbon-chart-waterfall" :hint="`${us(fall.total)} · ${fall.rows.length} statement runs`" flush>
        <template #actions>
          <span :class="REASON_CHIP[trace.reason]">{{ trace.reason }}</span>
          <ChartLegend :items="phaseLegend" />
        </template>
        <div class="px-4 py-3">
          <Waterfall :fall="fall" />
        </div>
      </SectionCard>
      <div v-else class="flex-1 flex items-center justify-center text-xs text-warm-400">Pick a trace.</div>
    </div>
    <ConfirmDialog :ask="ask" @close="ask = null" />
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue"

import { api } from "@/api/client"
import ChartLegend from "@/components/charts/ChartLegend.vue"
import SidePane from "@/components/shell/SidePane.vue"
import ConfirmDialog from "@/components/ui/ConfirmDialog.vue"
import SectionCard from "@/components/ui/SectionCard.vue"
import Segmented from "@/components/ui/Segmented.vue"
import { useCompact } from "@/composables/useCompact"
import { useLoad } from "@/composables/useLoad"
import { usePrograms } from "@/composables/usePrograms"
import { useLiveStore } from "@/stores/live"
import { useRunStore } from "@/stores/run"
import { clock, us } from "@/utils/format"
import { PHASE_LABEL, PHASES, phase } from "@/utils/palette"
import { waterfall } from "@/utils/waterfall"
import Waterfall from "@/views/jobs/Waterfall.vue"

const REASONS = [
  ["", "All"],
  ["failed", "Failed"],
  ["slowest", "Slowest"],
  ["sampled", "Sampled"],
]
const REASON_CHIP = { failed: "chip-coral", slowest: "chip-amber", sampled: "chip-warm" }

const run = useRunStore()
const compact = useCompact()
const { index } = usePrograms()
const reason = ref("")
const key = ref(null)

const { data: traces } = useLoad((r) => api.traces(r.runId, r.t0, r.t1 + 1, reason.value || null, 300), () => reason.value, [])
watch(traces, (ts) => {
  if (!ts?.some((t) => t.key === key.value)) key.value = ts?.[0]?.key ?? null
})
const { data: trace } = useLoad((r) => (key.value ? api.trace(r.runId, key.value) : null), () => key.value)

const fall = computed(() => waterfall(trace.value, index.value, run.laneName, run.unitName))

// The live machine's resident jobs, and the command a button proposes
const SHOWN = 50
const ACTIONS = {
  pause_job: { title: "Pause", note: "Its ready statements are held and nothing more is fetched for it; what is in flight finishes.", from: "running", to: "paused" },
  resume_job: { title: "Resume", note: "Its held statements issue and fetching goes on.", from: "paused", to: "running" },
  checkpoint_job: { title: "Checkpoint", note: "A checkpoint of the job is written to the machine's store; the answer is its status (a machine without a store answers failed, no_store).", from: null, to: "checkpoint" },
  cancel_job: { title: "Cancel", note: "The job ends with a Cancelled failure, and the sink gets it.", from: "resident", to: "cancelled", danger: true },
}
const live = useLiveStore()
const ask = ref(null)
const resident = computed(() => [...(live.now.jobs || [])].sort((a, b) => a.id - b.id))

function propose(j, command) {
  const a = ACTIONS[command]
  ask.value = {
    title: `${a.title} job ${j.id}`,
    command,
    body: { id: j.id },
    changes: [{ label: `job ${j.id}`, from: a.from ?? (j.paused ? "paused" : "running"), to: a.to }],
    note: a.note,
    confirm: `${a.title} job`,
    danger: !!a.danger,
  }
}
const phaseLegend = computed(() => (void run.theme, PHASES.map((p) => ({ label: PHASE_LABEL[p], color: phase(p) }))))
</script>
