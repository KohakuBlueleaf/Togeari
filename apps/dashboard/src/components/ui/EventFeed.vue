<template>
  <div class="flex flex-col">
    <div v-for="(e, i) in shown" :key="`${e.t}-${i}`" class="grid grid-cols-[4.5rem_auto_minmax(0,1fr)] items-center gap-2 px-4 h-8 text-xs kt-v2-line border-b last:border-b-0">
      <span class="num text-warm-500">{{ clock(e.t) }}</span>
      <span class="inline-flex items-center gap-1 font-medium"><span :class="ICON[RARE[e.kind]]" :style="{ color: tone(e.kind) }" class="text-[11px]" />{{ e.kind }}</span>
      <span class="num text-warm-600 dark:text-warm-300 truncate" :title="detail(e)">{{ detail(e) }}</span>
    </div>
    <p v-if="!shown.length" class="px-4 py-4 text-xs text-warm-400 flex items-center gap-1.5"><span class="i-carbon-checkmark-filled text-sage" />No unit, lane, plugin or machine event since the page opened.</p>
  </div>
</template>

<script setup>
import { computed } from "vue"

import { RARE } from "@/stores/live"
import { useThemeStore } from "@/stores/theme"
import { clock } from "@/utils/format"
import { status } from "@/utils/palette"

/** The live machine's rare events, newest first: time, kind (status tone and icon), fields. */
const props = defineProps({
  events: { type: Array, default: () => [] },
  limit: { type: Number, default: 12 },
})

const ICON = { good: "i-carbon-checkmark-filled", bad: "i-carbon-error-filled", warn: "i-carbon-warning-alt-filled", idle: "i-carbon-dot-mark" }
const theme = useThemeStore()
const shown = computed(() => props.events.slice(0, props.limit))
const tone = (kind) => (void theme.dark, status(RARE[kind] || "idle"))
const detail = (e) =>
  Object.entries(e)
    .filter(([k]) => k !== "kind" && k !== "t")
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join("  ")
</script>
