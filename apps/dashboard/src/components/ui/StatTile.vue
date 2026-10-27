<template>
  <div class="kt-v2-card px-4 py-3 min-w-0">
    <div class="section-title flex items-center gap-1.5"><span v-if="icon" :class="icon" class="text-[12px]" />{{ label }}</div>
    <div class="mt-1 flex items-baseline gap-2">
      <span class="num text-[22px] leading-7 font-semibold text-warm-800 dark:text-warm-100">{{ value }}</span>
      <span v-if="unit" class="text-xs text-warm-500">{{ unit }}</span>
    </div>
    <div class="mt-0.5 flex items-center gap-2 text-[11px] text-warm-500 min-h-4">
      <span v-if="status" :class="STATUS_CHIP[status]"> <span :class="STATUS_ICON[status]" class="text-[11px]" />{{ statusText }} </span>
      <span class="truncate">{{ note }}</span>
    </div>
    <Spark v-if="trend && trend.length > 1" :values="trend" :width="220" :height="26" class="mt-2 w-full" />
  </div>
</template>

<script setup>
import Spark from "@/components/charts/Spark.vue"

/** A headline number: label, value with unit, a note, an optional status chip and trend. */
defineProps({
  label: { type: String, required: true },
  value: { type: String, default: "—" },
  unit: { type: String, default: null },
  note: { type: String, default: null },
  icon: { type: String, default: null },
  status: { type: String, default: null },
  statusText: { type: String, default: null },
  trend: { type: Array, default: null },
})

const STATUS_CHIP = { good: "chip-sage", warn: "chip-amber", bad: "chip-coral" }
const STATUS_ICON = { good: "i-carbon-checkmark-filled", warn: "i-carbon-warning-alt-filled", bad: "i-carbon-error-filled" }
</script>
