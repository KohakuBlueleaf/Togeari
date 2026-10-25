/**
 * Screenshots of every view, both themes, against running servers: `npm run shots -- <out dir>
 * [filter]` with `togeari dash dev-runs` and `npm run dev` up (DASH_BASE, default
 * http://localhost:5190). Drives the installed Microsoft Edge headless; fails when a page logs
 * an error.
 */

import { mkdirSync, mkdtempSync, rmSync } from "node:fs"
import os from "node:os"
import path from "node:path"

import { chromium } from "playwright-core"

const OUT = path.resolve(process.argv[2] || "shots")
const BASE = process.env.DASH_BASE || "http://localhost:5190"
// The GBC run's id (DASH_GBC_RUN): its telemetry directory under the dev runs
const GBC = process.env.DASH_GBC_RUN || "gbc-5090-defaults-0c48b143"

const PHONE = { width: 390, height: 844 }
const pane = (name, state) => ({ [`togeari.dash.pane.${name}`]: JSON.stringify(state) })

/** [file name, URL query, options: viewport, saved localStorage, a selector clicked first,
 * the selectors double-clicked in turn (calls opened); for a live run (`live`): the steps taken
 * in turn (`steps`: [action, selector, text?], action click | fill | select) and the machine
 * events waited for (`events`, for rules on the timeline)] */
const LIVE = "run=live"
const KNOB = (name) => `.py-1:has(> div > span:text-is("${name}"))`
const SHOTS = [
  // A live machine (record_runs.py --live): read through the inspector, changed by commands
  ["live-overview", `${LIVE}&view=overview`, { live: true, events: 2 }],
  ["live-machine-dispatcher", `${LIVE}&view=machine&part=dispatcher`, { live: true }],
  ["live-machine-detect", `${LIVE}&view=machine&part=lane:detect`, { live: true }],
  ["live-machine-unit", `${LIVE}&view=machine&part=unit:tagger0`, { live: true }],
  ["live-knob-edit", `${LIVE}&view=machine&part=dispatcher`, { live: true, steps: [["click", `${KNOB("window")} button`], ["fill", `${KNOB("window")} input`, "0"]] }],
  ["live-knob-confirm", `${LIVE}&view=machine&part=dispatcher`, { live: true, steps: [["click", `${KNOB("window")} button`], ["fill", `${KNOB("window")} input`, "48"], ["click", `${KNOB("window")} button:text-is("Set")`]] }],
  [
    "live-knob-applied",
    `${LIVE}&view=machine&part=dispatcher`,
    { live: true, steps: [["click", `${KNOB("window")} button`], ["fill", `${KNOB("window")} input`, "48"], ["click", `${KNOB("window")} button:text-is("Set")`], ["click", 'button:text-is("Apply")']] },
  ],
  ["live-policy-confirm", `${LIVE}&view=machine&part=lane:detect`, { live: true, steps: [["select", "select >> nth=0", "detect-large"]] }],
  ["live-plugin-confirm", `${LIVE}&view=machine&part=dispatcher`, { live: true, steps: [["click", 'button[title="Disable dev-policies"]']] }],
  ["live-jobs", `${LIVE}&view=jobs`, { live: true }],
  ["live-job-cancel", `${LIVE}&view=jobs`, { live: true, steps: [["click", 'button[title="Cancel"] >> nth=0']] }],
  ["live-timeline", `${LIVE}&view=timeline&range=5m`, { live: true, events: 2 }],
  ["dark-live-machine", `${LIVE}&view=machine&part=dispatcher&theme=dark`, { live: true }],
  ["phone-live-jobs", `${LIVE}&view=jobs`, { live: true, viewport: PHONE }],

  // The GBC machine on one RTX 5090 (the real workload)
  ["gbc-machine", `run=${GBC}&view=machine`],
  ["gbc-machine-vlm", `run=${GBC}&view=machine&part=lane:vlm`],
  ["gbc-machine-vlm-unit", `run=${GBC}&view=machine&part=unit:vllm-0`],
  ["gbc-machine-detect", `run=${GBC}&view=machine&part=lane:detect`],
  ["gbc-machine-dispatcher", `run=${GBC}&view=machine&part=dispatcher`],
  ["dark-gbc-machine-pool", `run=${GBC}&view=machine&part=pool&theme=dark`],
  ["gbc-overview", `run=${GBC}&view=overview`],
  ["gbc-program-flowchart", `run=${GBC}&view=program`],
  ["gbc-program-node", `run=${GBC}&view=program&graph=nodes`],
  ["gbc-program-block", `run=${GBC}&view=program&graph=blocks`],
  ["gbc-program-open-root", `run=${GBC}&view=program`, { open: ['.vue-flow__node >> text="root"'] }],
  ["gbc-program-open-ask", `run=${GBC}&view=program`, { open: ['.vue-flow__node >> text="root"', '.vue-flow__node >> text="ask" >> nth=0'] }],
  ["gbc-program-node-open-root", `run=${GBC}&view=program&graph=nodes`, { open: ['.vue-flow__node >> text="root"'] }],
  ["gbc-jobs", `run=${GBC}&view=jobs`],
  ["gbc-timeline", `run=${GBC}&view=timeline`],
  ["dark-gbc-overview", `run=${GBC}&view=overview&theme=dark`],
  // The generic pipeline: item failures and an outage
  ["overview-flaky", "run=flaky&view=overview"],
  ["program-flaky-caption", "run=flaky&view=program&node=body/4/body/1"],
  ["machine-flaky", "run=flaky&view=machine"],
  ["dark-machine-saturated", "run=saturated&view=machine&theme=dark"],
  // Side regions folded and resized, as saved
  ["gbc-program-folded", `run=${GBC}&view=program`, { storage: { ...pane("rail", { collapsed: true }), ...pane("program", { collapsed: true }) } }],
  ["gbc-program-wide-panel", `run=${GBC}&view=program&node=body/24/body/1`, { storage: { ...pane("rail", { width: 300 }), ...pane("program", { width: 600 }) } }],
  ["gbc-jobs-folded", `run=${GBC}&view=jobs`, { storage: pane("jobs", { collapsed: true }) }],
  // A phone
  ["phone-gbc-overview", `run=${GBC}&view=overview`, { viewport: PHONE }],
  ["phone-gbc-machine", `run=${GBC}&view=machine&part=lane:vlm`, { viewport: PHONE }],
  ["phone-gbc-program", `run=${GBC}&view=program`, { viewport: PHONE }],
  ["phone-gbc-jobs", `run=${GBC}&view=jobs`, { viewport: PHONE }],
  ["phone-gbc-timeline", `run=${GBC}&view=timeline`, { viewport: PHONE }],
  ["phone-runs-drawer", `run=${GBC}&view=overview`, { viewport: PHONE, click: 'button[title="Runs"]' }],
]
const DESKTOP = { width: 1600, height: 1000 }

async function main() {
  mkdirSync(OUT, { recursive: true })
  const dir = mkdtempSync(path.join(os.tmpdir(), "togeari-dash-shots-"))
  const context = await chromium.launchPersistentContext(dir, { channel: "msedge", headless: true, viewport: DESKTOP })
  const page = context.pages()[0] || (await context.newPage())
  const errors = []
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`))
  page.on("console", (m) => {
    if (m.type() === "error" && !m.location().url.endsWith("/favicon.ico")) errors.push(`console: ${m.text()}`)
  })
  page.setDefaultTimeout(20000)
  let failed = 0
  try {
    for (const [name, query, opts = {}] of process.argv[3] ? SHOTS.filter(([n]) => n.includes(process.argv[3])) : SHOTS) {
      const before = errors.length
      await page.setViewportSize(opts.viewport || DESKTOP)
      // Saved pane state is read when the page loads: set it on the origin first
      await page.goto(`${BASE}/`)
      await page.evaluate((storage) => {
        localStorage.clear()
        for (const [k, v] of Object.entries(storage)) localStorage.setItem(k, v)
      }, opts.storage || {})
      await page.goto(`${BASE}/?${query}`)
      await page.waitForFunction(() => window.__dash?.run?.info)
      if (opts.live) {
        // A live page never goes network-idle (the event stream, the polls): wait for answers
        await page.waitForFunction(() => window.__dash.live.now.stats && window.__dash.live.now.knob_specs)
        if (opts.events) await page.waitForFunction((n) => window.__dash.live.events.length >= n, opts.events, { timeout: 90000 })
        await page.waitForTimeout(1500)
      } else await page.waitForLoadState("networkidle")
      // A recorded program after inlining is one graph of several hundred nodes: its layout takes
      // tens of seconds
      const started = Date.now()
      await page.waitForFunction(() => !document.querySelector(".kg-flow__status .kg-spin"), null, { timeout: 180000 })
      const layoutS = (Date.now() - started) / 1000
      if (opts.click) await page.click(opts.click)
      for (const [action, target, text] of opts.steps || []) {
        if (action === "click") await page.click(target)
        else if (action === "fill") await page.fill(target, text)
        else if (action === "select") await page.selectOption(target, text)
        await page.waitForTimeout(400)
      }
      for (const target of opts.open || []) {
        await page.dblclick(target)
        await page.waitForTimeout(300)
        await page.waitForFunction(() => !document.querySelector(".kg-flow__status .kg-spin"), null, { timeout: 180000 })
      }
      await page.waitForTimeout(900)
      const file = path.join(OUT, `${name}.png`)
      await page.screenshot({ path: file, fullPage: false })
      const errs = errors.slice(before)
      if (errs.length) failed++
      console.log(`${errs.length ? "ERR " : "ok  "} ${file} (ready after ${layoutS.toFixed(1)} s)${errs.length ? `\n      ${errs.join("\n      ")}` : ""}`)
    }
  } finally {
    await context.close()
    rmSync(dir, { recursive: true, force: true })
  }
  return failed ? 1 : 0
}

process.exit(await main())
