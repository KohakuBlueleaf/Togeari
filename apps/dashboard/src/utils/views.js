/** The views of a run, each a route `/run/<id>/<view>`: [view, label, icon]. */
export const VIEWS = [
  ["overview", "Overview", "i-carbon-dashboard"],
  ["program", "Program", "i-carbon-flow"],
  ["machine", "Machine", "i-carbon-chip"],
  ["jobs", "Jobs", "i-carbon-list-boxes"],
  ["timeline", "Timeline", "i-carbon-chart-line"],
]

export const DEFAULT_VIEW = "overview"

/** The view a route shows (its path `/run/<id>/<view>`), or null off a run's views. */
export function viewOf(route) {
  const view = route.path.split("/")[3]
  return VIEWS.some(([v]) => v === view) ? view : null
}

/** The path of `view` of run `id`. */
export const viewPath = (id, view = DEFAULT_VIEW) => `/run/${encodeURIComponent(id)}/${view}`
