<template>
  <div class="flex items-center gap-2 min-w-0" :title="title">
    <div class="flex-1 min-w-0 h-1.5 rounded-full bg-[var(--v2-line)] overflow-hidden">
      <div class="h-full rounded-full transition-[width] duration-300" :style="{ width: `${Math.round(clamped * 100)}%`, background: color }" />
    </div>
    <span v-if="label !== null" class="num text-[10px] text-warm-500 shrink-0 w-9 text-right">{{ label }}</span>
  </div>
</template>

<script setup>
import { computed } from "vue"

/** A thin fill gauge: `value` in 0..1 (null draws empty), its label to the right. */
const props = defineProps({
  value: { type: Number, default: null },
  color: { type: String, default: "var(--kt-color-iolite)" },
  label: { type: String, default: null },
  title: { type: String, default: "" },
})

const clamped = computed(() => (props.value === null || !Number.isFinite(props.value) ? 0 : Math.min(1, Math.max(0, props.value))))
</script>
