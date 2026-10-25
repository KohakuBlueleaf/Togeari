<template>
  <button
    class="seg-btn flex items-center gap-1"
    :class="run.withWait ? 'seg-btn-on' : ''"
    :aria-pressed="run.withWait"
    title="Count the time statements wait for their operands (the time their inputs' statements took, counted again)"
    @click="run.withWait = !run.withWait"
  >
    <span :class="run.withWait ? 'i-carbon-checkbox-checked' : 'i-carbon-checkbox'" class="text-[12px]" />Wait
  </button>
</template>

<script setup>
import { useRunStore } from "@/stores/run"

/** Whether statement time counts the wait for operands (`run.withWait`, kept in the URL). */
const run = useRunStore()
</script>
