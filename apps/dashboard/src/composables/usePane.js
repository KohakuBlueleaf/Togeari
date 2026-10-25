/**
 * A side region's width and collapsed state, kept in localStorage under `togeari.dash.pane.<name>`
 * and shared by every user of the same name (the rail and its phone drawer read one state).
 * `edge` is the side of the pane its drag handle sits on: "right" for a pane left of the content
 * (dragging right widens it), "left" for one on the right.
 */

import { reactive } from "vue"

const PANES = new Map()

function read(key, fallback) {
  try {
    const raw = localStorage.getItem(key)
    return raw === null ? fallback : JSON.parse(raw)
  } catch {
    return fallback
  }
}

function write(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* a private window or blocked storage: the pane still works, unpersisted */
  }
}

export function usePane(name, { initial, min, max, edge }) {
  if (PANES.has(name)) return PANES.get(name)
  const key = `togeari.dash.pane.${name}`
  const clamp = (w) => Math.max(min, Math.min(max, Math.round(w)))
  const saved = read(key, {})
  const pane = reactive({
    width: clamp(Number.isFinite(saved.width) ? saved.width : initial),
    collapsed: saved.collapsed === true,
    dragging: false,
    min,
    max,
    edge,
    persist() {
      write(key, { width: pane.width, collapsed: pane.collapsed })
    },
    toggle() {
      pane.collapsed = !pane.collapsed
      pane.persist()
    },
    reset() {
      pane.width = initial
      pane.persist()
    },
    startDrag(e) {
      e.preventDefault()
      pane.dragging = true
      const x0 = e.clientX
      const w0 = pane.width
      const sign = edge === "right" ? 1 : -1
      const move = (ev) => {
        pane.width = clamp(w0 + sign * (ev.clientX - x0))
      }
      const up = () => {
        pane.dragging = false
        document.removeEventListener("pointermove", move)
        document.removeEventListener("pointerup", up)
        document.removeEventListener("pointercancel", up)
        document.body.style.cursor = ""
        pane.persist()
      }
      document.body.style.cursor = "col-resize"
      document.addEventListener("pointermove", move)
      document.addEventListener("pointerup", up)
      document.addEventListener("pointercancel", up)
    },
  })
  PANES.set(name, pane)
  return pane
}
