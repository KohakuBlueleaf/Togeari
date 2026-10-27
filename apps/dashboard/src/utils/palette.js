/**
 * Chart colours, by the job each does (checked with the dataviz validator against the card
 * surface of each mode).
 *
 * - categorical (identity): iolite, then aquamarine; every pair separates under colour-vision
 *   deficiency in both modes. A third series is drawn as a small multiple, never a new hue;
 * - phases of a call: wait is a stone neutral (time nothing works on the call; it fails the
 *   chroma floor on purpose and is always labelled), queue iolite, service aquamarine;
 * - status (state) is reserved: sage good, amber warning, coral bad, always with an icon.
 */

const CATEGORICAL = {
  light: ["#5A4FCF", "#1F9A80"],
  dark: ["#8076E6", "#25A88E"],
}

const PHASE = {
  light: { wait: "#C4BBB0", queue: "#5A4FCF", service: "#1F9A80" },
  dark: { wait: "#5F5852", queue: "#8076E6", service: "#25A88E" },
}

export const STATUS = {
  light: { good: "#3A8A55", warn: "#B87A00", bad: "#C24A4A", idle: "#A09A92" },
  dark: { good: "#6DB585", warn: "#D9A23A", bad: "#E07A7A", idle: "#6A645F" },
}

export const PHASES = ["wait", "queue", "service"]

export const PHASE_LABEL = {
  wait: "Wait",
  queue: "Queue",
  service: "Service",
}

export const PHASE_NOTE = {
  wait: "fetched → issued (inputs, gates, admission)",
  queue: "enqueued on the lane → sent to a unit",
  service: "sent → result (inline: run time)",
}

export function mode() {
  return document.documentElement.classList.contains("dark") ? "dark" : "light"
}

export function series(i, m = mode()) {
  return CATEGORICAL[m][i % CATEGORICAL[m].length]
}

export function phase(name, m = mode()) {
  return PHASE[m][name]
}

export function status(name, m = mode()) {
  return STATUS[m][name] ?? STATUS[m].idle
}

/** Axis, grid and ink colours for uPlot in the current mode. */
export function chrome(m = mode()) {
  return m === "dark"
    ? {
        axis: "#8a8078",
        grid: "rgba(255,255,255,0.06)",
        tick: "rgba(255,255,255,0.10)",
        ink: "#e8e0d8",
        band: "rgba(128,118,230,0.14)",
      }
    : {
        axis: "#77706a",
        grid: "rgba(36,33,29,0.07)",
        tick: "rgba(36,33,29,0.14)",
        ink: "#2e2a26",
        band: "rgba(90,79,207,0.12)",
      }
}
