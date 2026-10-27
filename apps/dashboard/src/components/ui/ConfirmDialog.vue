<template>
  <Teleport to="body">
    <div v-if="ask" class="kt-v2 kg-theme fixed inset-0 z-50 flex items-center justify-center p-4 text-warm-800 dark:text-warm-100" @keydown.esc="close">
      <div class="absolute inset-0 bg-black/40" @click="close" />
      <div ref="box" role="dialog" aria-modal="true" :aria-label="ask.title" tabindex="-1" class="relative kt-v2-card shadow-xl w-[420px] max-w-full outline-none">
        <header class="card-head kt-v2-line">
          <span :class="ask.danger ? 'i-carbon-warning-alt text-coral' : 'i-carbon-settings-adjust text-iolite dark:text-iolite-light'" class="text-[15px]" />
          <h3 class="card-title truncate">{{ ask.title }}</h3>
          <button class="btn-icon ml-auto" title="Close" @click="close"><span class="i-carbon-close" /></button>
        </header>
        <div class="px-4 py-3 flex flex-col gap-2.5">
          <p v-if="ask.note" class="text-xs text-warm-600 dark:text-warm-300 leading-relaxed">{{ ask.note }}</p>
          <table v-if="ask.changes?.length" class="w-full text-xs">
            <tbody>
              <tr v-for="c in ask.changes" :key="c.label" class="kt-v2-line border-b last:border-b-0">
                <td class="py-1.5 pr-2 text-warm-500">{{ c.label }}</td>
                <td class="py-1.5 num text-right text-warm-500 line-through decoration-warm-400/60 truncate max-w-[9rem]" :title="text(c.from)">{{ text(c.from) }}</td>
                <td class="py-1.5 px-1.5 text-warm-400"><span class="i-carbon-arrow-right text-[11px]" /></td>
                <td class="py-1.5 num font-semibold truncate max-w-[9rem]" :title="text(c.to)">{{ text(c.to) }}</td>
              </tr>
            </tbody>
          </table>
          <div class="text-[11px] text-warm-400 num truncate" :title="call">{{ call }}</div>
          <div v-if="done" class="chip-sage !text-xs !py-1.5 !px-2.5 !rounded-lg whitespace-normal break-all"><span class="i-carbon-checkmark-filled shrink-0" />answered {{ text(answer) }}</div>
          <div v-if="failed" class="chip-coral !text-xs !py-1.5 !px-2.5 !rounded-lg whitespace-normal break-all"><span class="i-carbon-error-filled shrink-0" />{{ failed }}</div>
        </div>
        <footer class="px-4 py-3 kt-v2-line border-t flex items-center justify-end gap-2">
          <button class="btn-ghost" @click="close">{{ done ? "Close" : "Cancel" }}</button>
          <button v-if="!done" class="btn-primary" :class="ask.danger ? '!bg-coral hover:!bg-coral-shadow' : ''" :disabled="sending" @click="send"><span v-if="sending" class="i-carbon-circle-dash animate-spin" />{{ ask.confirm || "Apply" }}</button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, ref, watch } from "vue"

import { useLiveStore } from "@/stores/live"

/**
 * A command behind a confirm step: what changes (old to new), the command it sends, then the
 * machine's answer or its error. `ask` is `{title, command, body, changes?: [{label, from, to}],
 * note?, confirm?, danger?}`, null when closed; `answered` fires with the answer.
 */
const props = defineProps({ ask: { type: Object, default: null } })
const emit = defineEmits(["close", "answered"])

const live = useLiveStore()
const box = ref(null)
const sending = ref(false)
const done = ref(false)
const answer = ref(null)
const failed = ref(null)

const call = computed(() => (props.ask ? `${props.ask.command} ${JSON.stringify(props.ask.body || {})}` : ""))
const text = (v) => (v === null || v === undefined ? "None" : typeof v === "object" ? JSON.stringify(v) : String(v))

watch(
  () => props.ask,
  (a) => {
    sending.value = false
    done.value = false
    answer.value = null
    failed.value = null
    if (a) nextTick(() => box.value?.focus())
  },
)

async function send() {
  sending.value = true
  failed.value = null
  try {
    answer.value = await live.command(props.ask.command, props.ask.body || {})
    done.value = true
    emit("answered", answer.value)
  } catch (e) {
    failed.value = String(e.message || e)
  } finally {
    sending.value = false
  }
}

function close() {
  if (!sending.value) emit("close")
}
</script>
