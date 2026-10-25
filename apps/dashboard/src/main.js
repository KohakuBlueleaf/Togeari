import { createApp } from "vue"
import { createPinia } from "pinia"
import elkWorker from "elkjs/lib/elk-worker.min.js?url"
import { configureElk } from "@kohaku/kir-graph"

import App from "./App.vue"

import "uno.css"
import "uplot/dist/uPlot.min.css"
import "@kohaku/kir-graph/style.css"
import "./styles/base.css"
import "./styles/surfaces.css"
import "./styles/charts.css"

// ELK lays graphs out in a worker, off the page's thread
configureElk({ workerUrl: elkWorker })

createApp(App).use(createPinia()).mount("#app")
