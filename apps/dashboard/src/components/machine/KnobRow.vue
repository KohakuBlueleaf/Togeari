<template>
  <div class="py-1 text-[11px] min-w-0">
    <div class="flex items-center gap-2 min-w-0">
      <span class="text-warm-500 truncate" :title="hint">{{ name }}</span>
      <span class="ml-auto num truncate" :title="shown">{{ shown }}</span>
      <button v-if="spec && spec.kind === 'bool'" class="kt-switch shrink-0" :class="spec.value ? 'kt-switch--on' : ''" :title="`Set ${name} to ${!spec.value}`" @click="emit('propose', !spec.value)" />
      <button v-else-if="spec" class="btn-icon !w-6 !h-6 shrink-0" :title="`Change ${name}`" @click="open = !open"><span :class="open ? 'i-carbon-close' : 'i-carbon-edit'" class="text-[12px]" /></button>
    </div>
    <form v-if="open && spec" class="mt-1 mb-1.5 flex items-center gap-1.5" @submit.prevent="submit">
      <template v-if="spec.kind === 'choice' || spec.kind === 'policy'">
        <select v-model="draft" class="kt-input flex-1 min-w-0">
          <option v-for="c in choices" :key="c" :value="c">{{ c }}</option>
        </select>
      </template>
      <input v-else v-model="draft" class="kt-input flex-1 min-w-0 num" :placeholder="placeholder" :disabled="none" />
      <label v-if="spec.nullable" class="flex items-center gap-1 text-warm-500 shrink-0"><input v-model="none" type="checkbox" />None</label>
      <button class="btn-primary !h-6 !px-2 shrink-0" :disabled="problem !== null" :title="problem || 'Review the change'">Set</button>
    </form>
    <p v-if="open && problem && draft !== ''" class="text-[10.5px] text-coral-shadow dark:text-coral-light">{{ problem }}</p>
    <p v-if="open && spec?.kind === 'policy' && !choices.length" class="text-[10.5px] text-warm-400">No plugin contributes a policy for this role.</p>
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue"

/**
 * One knob of a live machine: its value, and an editor by its spec's kind (the `knob_specs`
 * query). `propose` fires with the parsed new value; the caller confirms and sends `set_knob`.
 */
const props = defineProps({
  name: { type: String, required: true },
  value: { default: null },
  spec: { type: Object, default: null },
  policies: { type: Object, default: () => ({}) },
})
const emit = defineEmits(["propose"])

const open = ref(false)
const draft = ref("")
const none = ref(false)

const shown = computed(() => {
  const v = props.spec ? props.spec.value : props.value
  return v === null || v === undefined ? "None" : typeof v === "object" ? JSON.stringify(v) : String(v)
})
const choices = computed(() => {
  const s = props.spec
  if (!s) return []
  return s.kind === "choice" ? s.choices : props.policies[s.role] || []
})
const hint = computed(() => {
  const s = props.spec
  if (!s) return props.name
  const bits = [s.kind]
  if (s.min !== undefined) bits.push(`>= ${s.min}`)
  if (s.nullable) bits.push("or None")
  return `${props.name}: ${bits.join(" ")}`
})
const placeholder = computed(() => ({ int: "an integer", number: "a number", rate: "0 … 1", ints: "e.g. 50000, 20, 100" })[props.spec?.kind] || "")

watch(open, (o) => {
  if (!o) return
  const v = props.spec?.value
  none.value = v === null
  draft.value = props.spec?.kind === "ints" && Array.isArray(v) ? v.join(", ") : v === null || v === undefined ? "" : String(v)
  if (props.spec?.kind === "policy") draft.value = choices.value[0] ?? ""
})

/** The draft parsed by the spec: `{value}` or `{problem}`. */
const parsed = computed(() => {
  const s = props.spec
  if (!s) return { problem: "no spec" }
  if (s.nullable && none.value) return { value: null }
  const t = String(draft.value).trim()
  const min = s.min ?? -Infinity
  switch (s.kind) {
    case "int": {
      if (!/^-?\d+$/.test(t)) return { problem: "an integer" }
      const n = Number(t)
      return n < min ? { problem: `at least ${min}` } : { value: n }
    }
    case "number":
    case "rate": {
      const n = Number(t)
      if (t === "" || !Number.isFinite(n)) return { problem: "a finite number" }
      if (s.kind === "rate" && (n < 0 || n > 1)) return { problem: "a rate in [0, 1]" }
      return n < min ? { problem: `at least ${min}` } : { value: n }
    }
    case "ints": {
      const parts = t.split(/[\s,]+/).filter(Boolean)
      if (!parts.length || parts.length > 3 || parts.some((p) => !/^\d+$/.test(p))) return { problem: "1 to 3 integers >= 0" }
      const ns = parts.map(Number)
      return ns[0] < 1 ? { problem: "the first at least 1" } : { value: ns }
    }
    case "choice":
    case "policy":
      return choices.value.includes(t) ? { value: t } : { problem: "pick one" }
  }
  return { problem: `unknown kind ${s.kind}` }
})
const problem = computed(() => parsed.value.problem ?? null)

function submit() {
  if (problem.value !== null) return
  emit("propose", parsed.value.value)
  open.value = false
}
</script>
