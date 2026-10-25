<template>
  <div class="relative w-full" @mouseleave="hover = false">
    <div class="h-3.5 flex" :style="{ width: `${Math.max(1.5, frac * 100)}%` }" @mouseenter="hover = true">
      <div
        v-for="p in parts"
        :key="p.name"
        class="h-full"
        :style="{ width: `${p.share * 100}%`, background: p.color, marginRight: p.last ? 0 : '2px', borderRadius: radius(p) }"
      />
    </div>
    <div v-if="hover" class="kt-tip kt-v2-float" style="top: 20px; left: 0">
      <div v-for="p in parts" :key="p.name" class="kt-tip__row">
        <span class="kt-tip__swatch" :style="{ background: p.color }" />{{ PHASE_LABEL[p.name] }}<span class="kt-tip__value">{{ us(p.value) }}</span>
      </div>
      <div class="kt-tip__row" style="opacity: 0.7">per call<span class="kt-tip__value">{{ us(total) }}</span></div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue"

import { useRunStore } from "@/stores/run"
import { us } from "@/utils/format"
import { PHASE_LABEL, PHASES, phase } from "@/utils/palette"

/**
 * One call's mean time split into wait, queue and service (µs), as a bar `frac` of the row
 * wide (its share of the largest row), a 2px gap between segments, rounded at the ends.
 */
const props = defineProps({
  wait: { type: Number, default: 0 },
  queue: { type: Number, default: 0 },
  service: { type: Number, default: 0 },
  frac: { type: Number, default: 1 },
})

const run = useRunStore()
const hover = ref(false)
const total = computed(() => (props.wait || 0) + (props.queue || 0) + (props.service || 0))
const parts = computed(() => {
  void run.theme
  const vals = { wait: props.wait || 0, queue: props.queue || 0, service: props.service || 0 }
  const shown = PHASES.filter((p) => vals[p] > 0)
  return shown.map((name, i) => ({
    name,
    value: vals[name],
    share: vals[name] / (total.value || 1),
    color: phase(name),
    first: i === 0,
    last: i === shown.length - 1,
  }))
})
const radius = (p) => `${p.first ? 4 : 0}px ${p.last ? 4 : 0}px ${p.last ? 4 : 0}px ${p.first ? 4 : 0}px`
</script>
