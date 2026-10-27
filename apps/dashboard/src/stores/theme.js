import { readPref, writePref } from "@/utils/prefs"

/** Light or dark (saved; the system's choice until one is made), set on `<html>`. */
export const useThemeStore = defineStore("theme", {
  state: () => ({ dark: false }),

  actions: {
    init() {
      const saved = readPref("theme", null)
      this.dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches
      this.apply()
    },

    set(dark) {
      this.dark = dark
      writePref("theme", dark ? "dark" : "light")
      this.apply()
    },

    toggle() {
      this.set(!this.dark)
    },

    apply() {
      document.documentElement.classList.toggle("dark", this.dark)
    },
  },
})
