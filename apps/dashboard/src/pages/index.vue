<template>
  <div class="h-full flex flex-col items-center justify-center gap-2 text-warm-500">
    <span class="i-carbon-data-vis-4 text-3xl text-warm-400" />
    <p v-if="runs.error" class="chip-coral !text-xs !py-1.5 !px-2.5"><span class="i-carbon-error-filled" />{{ runs.error }}</p>
    <p v-else-if="runs.loaded" class="text-sm">No runs found. Runs are directories with a <code class="text-xs">machine.json</code> under the dashboard's roots.</p>
    <p v-else class="text-sm">Looking for runs…</p>
  </div>
</template>

<script setup>
import { watch } from "vue"
import { useRouter } from "vue-router"

import { useRunStore } from "@/stores/run"
import { useRunsStore } from "@/stores/runs"
import { viewPath } from "@/utils/views"

/** No run named: the newest one, once the listing has it. */
const router = useRouter()
const run = useRunStore()
const runs = useRunsStore()

run.close()
watch(
  () => runs.list,
  (list) => {
    if (list.length) router.replace(viewPath(list[0].id))
  },
  { immediate: true },
)
</script>
