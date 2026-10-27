/**
 * The page's saved preferences: JSON in localStorage under `togeari.dash.<name>`. A private
 * window or blocked storage reads the fallback and keeps changes for the visit only.
 */

const PREFIX = "togeari.dash."

export function readPref(name, fallback) {
  try {
    const raw = localStorage.getItem(PREFIX + name)
    return raw === null ? fallback : JSON.parse(raw)
  } catch {
    return fallback
  }
}

export function writePref(name, value) {
  try {
    localStorage.setItem(PREFIX + name, JSON.stringify(value))
  } catch {
    /* unpersisted: the value still holds for this visit */
  }
}
