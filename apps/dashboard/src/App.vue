<template>
  <div class="kt-v2 kg-theme h-full flex text-warm-800 dark:text-warm-100">
    <RunRail v-if="!compact" />
    <Teleport to="body">
      <div v-if="compact && drawer" class="kt-v2 kg-theme fixed inset-0 z-50 flex text-warm-800 dark:text-warm-100">
        <div class="absolute inset-0 bg-black/40" @click="drawer = false" />
        <RunRail drawer class="relative h-full w-72 max-w-[85vw] shadow-xl" @close="drawer = false" />
      </div>
    </Teleport>
    <div class="flex-1 flex flex-col min-w-0">
      <TabStrip @menu="drawer = true" />
      <main class="kt-v2-canvas flex-1 min-h-0 overflow-auto">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<script setup>
import { ref } from "vue"
import { useRouter } from "vue-router"

import RunRail from "@/components/shell/RunRail.vue"
import TabStrip from "@/components/shell/TabStrip.vue"
import { useCompact } from "@/composables/useCompact"
import { useLiveStore } from "@/stores/live"
import { usePrefsStore } from "@/stores/prefs"
import { useRangeStore } from "@/stores/range"
import { useRunStore } from "@/stores/run"
import { useRunsStore } from "@/stores/runs"
import { useSelectionStore } from "@/stores/selection"
import { useThemeStore } from "@/stores/theme"

const compact = useCompact()
// The phone's run drawer
const drawer = ref(false)

useThemeStore().init()
// The listing probes every run that claims to be live: the routed run opens without waiting
useRunsStore().load()

// The dev server exposes the stores and the router to the screenshot script
if (import.meta.env.DEV) {
  window.__dash = {
    run: useRunStore(),
    runs: useRunsStore(),
    live: useLiveStore(),
    range: useRangeStore(),
    selection: useSelectionStore(),
    prefs: usePrefsStore(),
    theme: useThemeStore(),
    router: useRouter(),
  }
}
</script>
