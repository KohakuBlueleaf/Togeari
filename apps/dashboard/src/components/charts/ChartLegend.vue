<template>
  <div v-if="items.length > 1" class="flex items-center gap-3 text-[11px] text-warm-600 dark:text-warm-300">
    <span v-for="it in items" :key="it.label" class="flex items-center gap-1.5">
      <span class="w-2.5 h-2.5 rounded-[3px]" :style="{ background: it.color }" />{{ it.label }}
    </span>
  </div>
</template>

<script setup>
/** A chart's legend (two series or more): a swatch and the label in text ink. */
defineProps({ items: { type: Array, default: () => [] } })
</script>
