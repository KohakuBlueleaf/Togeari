import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"
import VueRouter from "unplugin-vue-router/vite"
import AutoImport from "unplugin-auto-import/vite"
import Components from "unplugin-vue-components/vite"
import { VueRouterAutoImports } from "unplugin-vue-router"
import UnoCSS from "unocss/vite"
import { fileURLToPath, URL } from "node:url"

// The dashboard server the dev server proxies to; set TOGEARI_DASH_API when it runs elsewhere.
const api = process.env.TOGEARI_DASH_API || "http://127.0.0.1:8787"
const DEV_ORIGINS = new Set(["http://localhost:5190", "http://127.0.0.1:5190"])

// The display library from a KohakuNodeIR checkout (its source), or the installed package
const kirGraph =
  process.env.KIR_GRAPH_SRC ||
  fileURLToPath(new URL("../../../KohakuNodeIR/packages/kir-graph/", import.meta.url))
const graphLib = (path) => `${kirGraph.replace(/[\\/]$/, "")}/${path}`

export default defineConfig({
  plugins: [
    VueRouter({ routesFolder: "src/pages", dts: false }),
    vue(),
    UnoCSS(),
    AutoImport({ imports: ["vue", "pinia", VueRouterAutoImports], dts: false }),
    Components({ dirs: ["src/components"], dts: false }),
  ],
  resolve: {
    alias: [
      { find: "@", replacement: fileURLToPath(new URL("./src", import.meta.url)) },
      { find: /^@kohaku\/kir-graph$/, replacement: graphLib("src/index.js") },
      { find: /^@kohaku\/kir-graph\/style\.css$/, replacement: graphLib("src/styles/index.css") },
    ],
    dedupe: [
      "vue",
      "@vue-flow/core",
      "@vue-flow/background",
      "@vue-flow/controls",
      "@vue-flow/minimap",
      "elkjs",
    ],
  },
  server: {
    port: 5190,
    proxy: {
      "/api": {
        target: api,
        changeOrigin: true,
        // The dash server accepts only its own origin: this dev server's pages are sent as it
        configure(proxy) {
          proxy.on("proxyReq", (req) => {
            const origin = req.getHeader("origin")
            if (origin && DEV_ORIGINS.has(String(origin).toLowerCase()))
              req.setHeader("origin", api)
          })
        },
      },
    },
  },
  build: {
    // The Python package serves the built app (togeari dash)
    outDir: fileURLToPath(new URL("../../src/togeari/dash/static", import.meta.url)),
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("elkjs")) return "elk"
          if (id.includes("@vue-flow")) return "vue-flow"
          if (id.includes("uplot")) return "uplot"
          if (
            id.includes("node_modules/vue") ||
            id.includes("node_modules/pinia") ||
            id.includes("node_modules/vue-router")
          )
            return "vue-vendor"
        },
      },
    },
  },
})
