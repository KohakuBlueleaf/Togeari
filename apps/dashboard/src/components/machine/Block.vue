<template>
  <div role="button" tabindex="0" class="kt-v2-card kt-block text-left flex flex-col min-w-0 min-h-0 px-3 py-2 transition-colors cursor-pointer" :class="[on ? 'kt-block--on' : '', accent ? `kt-block--${accent}` : '']" :aria-pressed="on" @click="$emit('pick')" @keydown.enter.prevent="$emit('pick')">
    <div class="flex items-center gap-1.5 min-w-0">
      <span v-if="icon" :class="icon" class="text-[13px] text-warm-500 dark:text-warm-400 shrink-0" />
      <span class="text-[12.5px] font-semibold truncate" :class="accent ? `kt-block__title--${accent}` : ''">{{ title }}</span>
      <span v-if="sub" class="text-[11px] text-warm-400 truncate">{{ sub }}</span>
      <div class="ml-auto flex items-center gap-1 shrink-0"><slot name="aside" /></div>
    </div>
    <div class="mt-1 flex-1 min-h-0 flex flex-col"><slot /></div>
  </div>
</template>

<script setup>
/**
 * One part of the machine picture: a card that opens the part's details in the side panel.
 * `accent` ("dispatcher", "sink") borders it as the teaser does; `on` marks the open part.
 */
defineProps({
  title: { type: String, required: true },
  sub: { type: String, default: null },
  icon: { type: String, default: null },
  accent: { type: String, default: null },
  on: { type: Boolean, default: false },
})
defineEmits(["pick"])
</script>

<style scoped>
.kt-block:hover {
  border-color: var(--v2-line-strong);
}
.kt-block--dispatcher {
  border-color: color-mix(in srgb, var(--kt-color-iolite) 55%, transparent);
  background: color-mix(in srgb, var(--kt-color-iolite) 4%, var(--v2-card));
}
.kt-block--sink {
  border-color: color-mix(in srgb, var(--kt-color-sage) 55%, transparent);
}
.kt-block__title--dispatcher {
  color: var(--kt-color-iolite);
}
:global(html.dark) .kt-block__title--dispatcher {
  color: var(--kt-color-iolite-light);
}
.kt-block--on,
.kt-block--on:hover {
  border-color: var(--kt-color-iolite);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--kt-color-iolite) 25%, transparent);
}
</style>
