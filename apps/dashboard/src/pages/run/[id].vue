<template>
  <div v-if="missing" class="m-6 chip-coral !text-xs !py-1.5 !px-2.5"><span class="i-carbon-error-filled" />No run "{{ key }}" under the dashboard's roots.</div>
  <div v-else-if="run.error" class="m-6 chip-coral !text-xs !py-1.5 !px-2.5"><span class="i-carbon-error-filled" />{{ run.error }}</div>
  <RouterView v-else-if="run.info && run.runId === key" :key="run.runId" />
</template>

<script setup>
import { computed, watch } from "vue"
import { useRoute, useRouter } from "vue-router"

import { useRunStore } from "@/stores/run"
import { useRunsStore } from "@/stores/runs"
import { viewOf, viewPath } from "@/utils/views"

/**
 * A run's views (`/run/<id>/<view>`): the route's run is the open one. A run given by its name
 * goes to its id once the listing has it; a run id opens without waiting for the listing.
 */
const route = useRoute()
const router = useRouter()
const run = useRunStore()
const runs = useRunsStore()
const key = computed(() => String(route.params.id))
const RUN_ID = /-[0-9a-f]{8}$/

const missing = computed(() => !RUN_ID.test(key.value) && runs.loaded && !runs.find(key.value))

watch(
  [key, () => runs.list],
  ([k]) => {
    const found = runs.find(k)
    if (found && found.id !== k) router.replace(viewPath(found.id, viewOf(route) || undefined))
    else if (found || RUN_ID.test(k)) run.open(k)
  },
  { immediate: true },
)
</script>
