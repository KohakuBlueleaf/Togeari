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
// Saved preferences (localStorage, `togeari.dash.<name>`), set on the origin before the page loads
const saved = (name, value) => ({ [`togeari.dash.${name}`]: JSON.stringify(value) })
const pane = (name, state) => saved(`pane.${name}`, state)
const DARK = saved("theme", "dark")
const graph = (view) => saved("graphView", view)

/** [file name, `<run id or name>/<view>`, options: viewport, saved localStorage (`storage`), the
 * Machine view's part and the Program view's node picked once loaded (`part`, `node`), a
 * selector clicked first, the selectors double-clicked in turn (calls opened); for a live run
 * (`live`): the steps taken in turn (`steps`: [action, selector, text?], action click | fill |
 * select) and the machine events waited for (`events`, for rules on the timeline)] */
const KNOB = (name) => `.py-1:has(> div > span:text-is("${name}"))`
const SHOTS = [
  // A live machine (record_runs.py --live): read through the inspector, changed by commands
  ["live-overview", "live/overview", { live: true, events: 2 }],
  ["live-machine-dispatcher", "live/machine", { live: true, part: "dispatcher" }],
  ["live-machine-detect", "live/machine", { live: true, part: "lane:detect" }],
  ["live-machine-unit", "live/machine", { live: true, part: "unit:tagger0" }],
  [
    "live-knob-edit",
    "live/machine",
    {
      live: true,
      part: "dispatcher",
      steps: [
        ["click", `${KNOB("window")} button`],
        ["fill", `${KNOB("window")} input`, "0"],
      ],
    },
  ],
  [
    "live-knob-confirm",
    "live/machine",
    {
      live: true,
      part: "dispatcher",
      steps: [
        ["click", `${KNOB("window")} button`],
        ["fill", `${KNOB("window")} input`, "48"],
        ["click", `${KNOB("window")} button:text-is("Set")`],
      ],
    },
  ],
  [
    "live-knob-applied",
    "live/machine",
    {
      live: true,
      part: "dispatcher",
      steps: [
        ["click", `${KNOB("window")} button`],
        ["fill", `${KNOB("window")} input`, "48"],
        ["click", `${KNOB("window")} button:text-is("Set")`],
        ["click", 'button:text-is("Apply")'],
      ],
    },
  ],
  [
    "live-policy-confirm",
    "live/machine",
    { live: true, part: "lane:detect", steps: [["select", "select >> nth=0", "detect-large"]] },
  ],
  [
    "live-plugin-confirm",
    "live/machine",
    { live: true, part: "dispatcher", steps: [["click", 'button[title="Disable dev-policies"]']] },
  ],
  ["live-jobs", "live/jobs", { live: true }],
  [
    "live-job-cancel",
    "live/jobs",
    { live: true, steps: [["click", 'button[title="Cancel"] >> nth=0']] },
  ],
  ["live-timeline", "live/timeline", { live: true, events: 2, storage: saved("preset", 300) }],
  ["dark-live-machine", "live/machine", { live: true, part: "dispatcher", storage: DARK }],
  ["phone-live-jobs", "live/jobs", { live: true, viewport: PHONE }],

  // The GBC machine on one RTX 5090 (the real workload)
  ["gbc-machine", `${GBC}/machine`],
  ["gbc-machine-vlm", `${GBC}/machine`, { part: "lane:vlm" }],
  ["gbc-machine-vlm-unit", `${GBC}/machine`, { part: "unit:vllm-0" }],
  ["gbc-machine-detect", `${GBC}/machine`, { part: "lane:detect" }],
  ["gbc-machine-dispatcher", `${GBC}/machine`, { part: "dispatcher" }],
  ["dark-gbc-machine-pool", `${GBC}/machine`, { part: "pool", storage: DARK }],
  ["gbc-overview", `${GBC}/overview`],
  ["gbc-program-flowchart", `${GBC}/program`],
  ["gbc-program-node", `${GBC}/program`, { storage: graph("nodes") }],
  ["gbc-program-block", `${GBC}/program`, { storage: graph("blocks") }],
  ["gbc-program-open-root", `${GBC}/program`, { open: ['.vue-flow__node >> text="root"'] }],
  [
    "gbc-program-open-ask",
    `${GBC}/program`,
    { open: ['.vue-flow__node >> text="root"', '.vue-flow__node >> text="ask" >> nth=0'] },
  ],
  [
    "gbc-program-node-open-root",
    `${GBC}/program`,
    { storage: graph("nodes"), open: ['.vue-flow__node >> text="root"'] },
  ],
  ["gbc-jobs", `${GBC}/jobs`],
  ["gbc-timeline", `${GBC}/timeline`],
  ["dark-gbc-overview", `${GBC}/overview`, { storage: DARK }],
  // The generic pipeline: item failures and an outage
  ["overview-flaky", "flaky/overview"],
  ["program-flaky-caption", "flaky/program", { node: "body/4/body/1" }],
  ["machine-flaky", "flaky/machine"],
  ["dark-machine-saturated", "saturated/machine", { storage: DARK }],
  // Side regions folded and resized, as saved
  [
    "gbc-program-folded",
    `${GBC}/program`,
    { storage: { ...pane("rail", { collapsed: true }), ...pane("program", { collapsed: true }) } },
  ],
  [
    "gbc-program-wide-panel",
    `${GBC}/program`,
    {
      node: "body/24/body/1",
      storage: { ...pane("rail", { width: 300 }), ...pane("program", { width: 600 }) },
    },
  ],
  ["gbc-jobs-folded", `${GBC}/jobs`, { storage: pane("jobs", { collapsed: true }) }],
  // A phone
  ["phone-gbc-overview", `${GBC}/overview`, { viewport: PHONE }],
  ["phone-gbc-machine", `${GBC}/machine`, { viewport: PHONE, part: "lane:vlm" }],
  ["phone-gbc-program", `${GBC}/program`, { viewport: PHONE }],
  ["phone-gbc-jobs", `${GBC}/jobs`, { viewport: PHONE }],
  ["phone-gbc-timeline", `${GBC}/timeline`, { viewport: PHONE }],
  ["phone-runs-drawer", `${GBC}/overview`, { viewport: PHONE, click: 'button[title="Runs"]' }],
]
const DESKTOP = { width: 1600, height: 1000 }

async function main() {
  mkdirSync(OUT, { recursive: true })
  const dir = mkdtempSync(path.join(os.tmpdir(), "togeari-dash-shots-"))
  const context = await chromium.launchPersistentContext(dir, {
    channel: "msedge",
    headless: true,
    viewport: DESKTOP,
  })
  const page = context.pages()[0] || (await context.newPage())
  const errors = []
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`))
  page.on("console", (m) => {
    if (m.type() === "error" && !m.location().url.endsWith("/favicon.ico"))
      errors.push(`console: ${m.text()}`)
  })
  page.setDefaultTimeout(20000)
  let failed = 0
  try {
    for (const [name, where, opts = {}] of process.argv[3]
      ? SHOTS.filter(([n]) => n.includes(process.argv[3]))
      : SHOTS) {
      const before = errors.length
      await page.setViewportSize(opts.viewport || DESKTOP)
      // Saved pane state is read when the page loads: set it on the origin first
      await page.goto(`${BASE}/`)
      await page.evaluate((storage) => {
        localStorage.clear()
        for (const [k, v] of Object.entries(storage)) localStorage.setItem(k, v)
      }, opts.storage || {})
      await page.goto(`${BASE}/run/${where}`)
      await page.waitForFunction(() => window.__dash?.run?.info)
      await page.evaluate(({ part, node }) => {
        if (part) window.__dash.selection.part = part
        if (node) window.__dash.selection.node = node
      }, opts)
      if (opts.live) {
        // A live page never goes network-idle (the event stream, the polls): wait for answers
        await page.waitForFunction(
          () => window.__dash.live.now.stats && window.__dash.live.now.knob_specs,
        )
        if (opts.events)
          await page.waitForFunction((n) => window.__dash.live.events.length >= n, opts.events, {
            timeout: 90000,
          })
        await page.waitForTimeout(1500)
      } else await page.waitForLoadState("networkidle")
      // A recorded program after inlining is one graph of several hundred nodes: its layout takes
      // tens of seconds
      const started = Date.now()
      await page.waitForFunction(() => !document.querySelector(".kg-flow__status .kg-spin"), null, {
        timeout: 180000,
      })
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
        await page.waitForFunction(
          () => !document.querySelector(".kg-flow__status .kg-spin"),
          null,
          { timeout: 180000 },
        )
      }
      await page.waitForTimeout(900)
      const file = path.join(OUT, `${name}.png`)
      await page.screenshot({ path: file, fullPage: false })
      const errs = errors.slice(before)
      if (errs.length) failed++
      console.log(
        `${errs.length ? "ERR " : "ok  "} ${file} (ready after ${layoutS.toFixed(1)} s)${errs.length ? `\n      ${errs.join("\n      ")}` : ""}`,
      )
    }
  } finally {
    await context.close()
    rmSync(dir, { recursive: true, force: true })
  }
  return failed ? 1 : 0
}

process.exit(await main())
