<template>
  <button class="seg-btn flex items-center gap-1" :class="prefs.withWait ? 'seg-btn-on' : ''" :aria-pressed="prefs.withWait" title="Count the time statements wait for their operands (the time their inputs' statements took, counted again)" @click="prefs.withWait = !prefs.withWait"><span :class="prefs.withWait ? 'i-carbon-checkbox-checked' : 'i-carbon-checkbox'" class="text-[12px]" />Wait</button>
</template>

<script setup>
import { usePrefsStore } from "@/stores/prefs"

/** Whether statement time counts the wait for operands (`prefs.withWait`, saved). */
const prefs = usePrefsStore()
</script>
