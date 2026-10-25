<template>
  <div class="flex items-center gap-0.5 p-0.5 rounded-lg bg-warm-200/50 dark:bg-warm-800/60">
    <button v-for="[value, label] in options" :key="value" class="seg-btn" :class="modelValue === value ? 'seg-btn-on' : ''" @click="emit('update:modelValue', value)">
      {{ label }}
    </button>
  </div>
</template>

<script setup>
/** A segmented control: `options` are `[value, label]` pairs. */
defineProps({
  modelValue: { type: [String, Number], default: null },
  options: { type: Array, required: true },
})
const emit = defineEmits(["update:modelValue"])
</script>
