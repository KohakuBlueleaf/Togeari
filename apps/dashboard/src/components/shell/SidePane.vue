<template>
  <!-- Phone: a section under (or over) the content that folds to its title bar -->
  <section v-if="compact" class="shrink-0 kt-v2-panel kt-v2-edge flex flex-col min-h-0" :class="[side === 'left' ? 'border-b' : 'border-t', pane.collapsed ? '' : 'max-h-[55vh]']">
    <button class="h-9 shrink-0 px-4 flex items-center gap-2 text-[12px] font-medium text-warm-600 dark:text-warm-300" :aria-expanded="!pane.collapsed" @click="pane.toggle()">
      <span :class="icon" class="text-warm-500" />{{ label }}
      <span class="ml-auto text-warm-400" :class="pane.collapsed === (side === 'left') ? 'i-carbon-chevron-down' : 'i-carbon-chevron-up'" />
    </button>
    <div v-if="!pane.collapsed" class="flex-1 min-h-0 flex flex-col" :class="scroll ? 'overflow-auto' : ''"><slot /></div>
  </section>

  <!-- Desktop: a fixed column, resizable from its inner edge, collapsible to a strip -->
  <aside v-else class="relative shrink-0 kt-v2-panel kt-v2-edge flex flex-col min-h-0" :class="side === 'right' ? 'border-l' : 'border-r'" :style="{ width: `${pane.collapsed ? STRIP : pane.width}px` }">
    <template v-if="pane.collapsed">
      <button class="btn-icon mx-auto mt-2" :title="`Show ${label}`" @click="pane.toggle()"><span :class="side === 'right' ? 'i-carbon-chevron-left' : 'i-carbon-chevron-right'" /></button>
      <button class="mt-2 mx-auto flex flex-col items-center gap-2 text-[11px] text-warm-500 hover:text-warm-700 dark:hover:text-warm-200" :title="`Show ${label}`" @click="pane.toggle()">
        <span :class="icon" />
        <span class="[writing-mode:vertical-rl]">{{ label }}</span>
      </button>
    </template>
    <template v-else>
      <div class="flex-1 min-h-0 flex flex-col" :class="scroll ? 'overflow-auto' : ''"><slot /></div>
      <div class="absolute top-0 h-full w-1.5 cursor-col-resize z-10 transition-colors hover:bg-iolite/30" :class="[side === 'right' ? '-left-[3px]' : '-right-[3px]', pane.dragging ? 'bg-iolite/50' : '']" title="Drag to resize · double-click to reset" @pointerdown="pane.startDrag" @dblclick="pane.reset()" />
      <button class="absolute top-2.5 z-20 w-5 h-5 rounded-full kt-v2-float flex items-center justify-center text-warm-500 hover:text-warm-800 dark:hover:text-warm-100" :class="side === 'right' ? '-left-2.5' : '-right-2.5'" :title="`Hide ${label}`" @click="pane.toggle()">
        <span :class="side === 'right' ? 'i-carbon-chevron-right' : 'i-carbon-chevron-left'" class="text-[11px]" />
      </button>
    </template>
  </aside>
</template>

<script setup>
import { useCompact } from "@/composables/useCompact"
import { usePane } from "@/composables/usePane"

/** A side region beside a view's content: `side` is where it sits; `name` keys its saved width. */
const props = defineProps({
  name: { type: String, required: true },
  side: { type: String, default: "right" },
  label: { type: String, required: true },
  icon: { type: String, default: "i-carbon-side-panel-open" },
  initial: { type: Number, default: 380 },
  min: { type: Number, default: 260 },
  max: { type: Number, default: 720 },
  scroll: { type: Boolean, default: true },
})

const STRIP = 36
const compact = useCompact()
const pane = usePane(props.name, { initial: props.initial, min: props.min, max: props.max, edge: props.side === "right" ? "left" : "right" })
</script>
