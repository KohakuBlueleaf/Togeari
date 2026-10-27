<template>
  <table class="w-full text-xs">
    <thead class="text-warm-400">
      <tr class="kt-v2-line border-b">
        <th class="text-left font-medium px-4 h-8">Statement</th>
        <th class="text-right font-medium px-3">Calls</th>
        <template v-if="metric === 'errors'">
          <th class="text-right font-medium px-3">Failed</th>
          <th class="text-right font-medium px-3">Rate</th>
          <th class="text-right font-medium px-4">Faults</th>
        </template>
        <template v-else>
          <th class="text-right font-medium px-3">p50</th>
          <th class="text-right font-medium px-3">p90</th>
          <th class="text-right font-medium px-4">p99</th>
        </template>
      </tr>
    </thead>
    <tbody>
      <tr v-for="r in rows" :key="r.addr" class="kt-v2-line border-b last:border-b-0 kt-row cursor-pointer" @click="emit('open', r)">
        <td class="px-4 h-9">
          <span class="font-medium">{{ r.node }}</span>
          <span class="num text-[10px] text-warm-400 ml-1.5"
            >@{{ r.addr }}<template v-if="r.line"> · L{{ r.line }}</template></span
          >
        </td>
        <td class="px-3 text-right num">{{ count(r.calls) }}</td>
        <template v-if="metric === 'errors'">
          <td class="px-3 text-right num" :class="r.errors ? 'text-coral-shadow dark:text-coral-light' : ''">{{ count(r.errors) }}</td>
          <td class="px-3 text-right num">{{ pct(r.errors / Math.max(1, r.calls), 2) }}</td>
          <td class="px-4 text-right num">{{ r.faults ? count(r.faults) : "—" }}</td>
        </template>
        <template v-else>
          <td class="px-3 text-right num">{{ us(r[metric]?.p50) }}</td>
          <td class="px-3 text-right num">{{ us(r[metric]?.p90) }}</td>
          <td class="px-4 text-right num font-medium">{{ us(r[metric]?.p99) }}</td>
        </template>
      </tr>
    </tbody>
  </table>
</template>

<script setup>
import { count, pct, us } from "@/utils/format"

/** Statements with their calls and a metric's quantiles, or their failures (`metric` "errors"). */
defineProps({
  rows: { type: Array, default: () => [] },
  metric: { type: String, default: "service" },
})
const emit = defineEmits(["open"])
</script>
