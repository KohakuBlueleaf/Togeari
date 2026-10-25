/**
 * Whether the window is phone-sized (below Tailwind's `md`, 768 px): the run rail becomes a
 * drawer and side panels stack under the content.
 */

import { ref } from "vue"

const QUERY = "(max-width: 767px)"
const compact = ref(false)

if (typeof window !== "undefined" && window.matchMedia) {
  const mq = window.matchMedia(QUERY)
  compact.value = mq.matches
  mq.addEventListener("change", (e) => {
    compact.value = e.matches
  })
}

export function useCompact() {
  return compact
}
