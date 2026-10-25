<template>
  <section class="kt-v2-card flex flex-col min-w-0">
    <header class="card-head kt-v2-line">
      <span v-if="icon" :class="icon" class="text-warm-500 dark:text-warm-400 text-[15px]" />
      <h3 class="card-title truncate">{{ title }}</h3>
      <span v-if="count !== null && count !== undefined" class="num text-[11px] text-warm-400">{{ count }}</span>
      <span v-if="hint" class="text-[11px] text-warm-400 truncate">{{ hint }}</span>
      <div class="ml-auto flex items-center gap-1.5 shrink-0">
        <slot name="actions" />
      </div>
    </header>
    <div class="flex-1 min-h-0" :class="flush ? '' : 'p-4'">
      <slot />
    </div>
  </section>
</template>

<script setup>
/** A KohakuTerrarium section card: a 40px header (icon, title, mono count, actions), a body. */
defineProps({
  title: { type: String, required: true },
  icon: { type: String, default: null },
  count: { type: [Number, String], default: null },
  hint: { type: String, default: null },
  flush: { type: Boolean, default: false },
})
</script>
