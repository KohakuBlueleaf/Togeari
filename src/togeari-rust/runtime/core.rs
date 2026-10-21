//! The dispatcher's state. No field is borrowed across a call into Python, so a plugin, a
//! listener or a view may read the core from inside any callback.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, VecDeque};
use std::rc::{Rc, Weak};
use std::sync::Arc;
use std::time::Instant;

use indexmap::{IndexMap, IndexSet};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet};

use crate::lanes::python::PyLane;
use crate::program::core::Code;

use super::gate::Gate;
use super::host::Host;
use super::inbox::Inbox;
use super::state::{Entry, Freed, InfRef, Inflight, JobRef, Obj};

/// Run times of one inline node: the last 1000, the count, the total, whether it was reported.
#[derive(Default)]
pub struct InlineStat {
    pub times: VecDeque<f64>,
    pub n: u64,
    pub total: f64,
    pub warned: bool,
}

impl InlineStat {
    pub fn add(&mut self, dt: f64) {
        if self.times.len() == 1000 {
            self.times.pop_front();
        }
        self.times.push_back(dt);
        self.n += 1;
        self.total += dt;
    }

    pub fn median(&self) -> f64 {
        let mut s: Vec<f64> = self.times.iter().copied().collect();
        s.sort_by(|a, b| a.partial_cmp(b).unwrap());
        s.get(s.len() / 2).copied().unwrap_or(0.0)
    }
}

/// The dispatcher's counters, in the Python core's key order.
#[derive(Default)]
pub struct Stats {
    pub jobs_ok: i64,
    pub jobs_failed: i64,
    pub calls: i64,
    pub squashed: i64,
    pub reused: i64,
    pub wasted: i64,
    pub store_hits: i64,
    pub store_misses: i64,
    pub store_weak: i64,
    pub store_errors: i64,
    pub snapshot_errors: i64,
    pub weak_digests: IndexMap<String, i64>,
    pub machine_errors: i64,
    pub records_blocked: i64,
    pub records_dropped: i64,
    pub cycles: i64,
    pub busy_s: f64,
    pub phase_s: IndexMap<&'static str, f64>,
    pub cycle_hist: IndexMap<u32, i64>,
    pub cycle_max_s: f64,
    pub extra: IndexMap<&'static str, i64>,
}

/// Region work so far: regions (each released or held, by the offer clock), steps fetched in
/// them and left for the GIL path, issue orderings and retires done in a region and left for the
/// GIL path; GIL offers (released regions and pure offers at a cycle's end).
#[derive(Default, Clone, Copy)]
pub struct RegionCounts {
    pub regions: u64,
    pub regions_released: u64,
    pub regions_held: u64,
    pub pure_offers: u64,
    pub offers: u64,
    pub free_steps: u64,
    pub gil_steps: u64,
    pub free_orders: u64,
    pub gil_orders: u64,
    pub free_retires: u64,
    pub gil_retires: u64,
}

pub struct Core {
    pub h: Host,
    pub code: RefCell<Code>,
    pub clock: RefCell<Obj>,
    pub epoch: Instant,

    // Job pool and scheduling sets; jobs to fetch for, in the order they became dirty
    pub jobs: RefCell<IndexMap<u64, JobRef>>,
    pub keys: Py<PySet>,
    pub ready: RefCell<Vec<InfRef>>,
    pub dirty: RefCell<IndexSet<u64>>,
    pub last_started: RefCell<Option<JobRef>>,
    pub blocked: RefCell<HashMap<u64, Vec<InfRef>>>,
    pub pool_bytes: Cell<i64>,
    pub freed: Freed,
    pub warned_keys: Cell<bool>,

    // Inflights a work item names (``WorkItem.owner``), by id
    pub owned: RefCell<HashMap<u64, Weak<RefCell<Inflight>>>>,
    pub owned_live: Cell<usize>,

    // The dispatcher instance (a weak reference), its inbox, entries by name, lanes by object
    pub shell: RefCell<Option<Obj>>,
    pub inbox: RefCell<Option<Py<Inbox>>>,
    pub entries: RefCell<HashMap<String, (Obj, Rc<Entry>)>>,
    pub lanes: RefCell<HashMap<usize, KnownLane>>,

    // Totals; inline nodes by name, and each node string's entry
    pub stats: RefCell<Stats>,
    pub inline: RefCell<IndexMap<String, InlineStat>>,
    pub inline_at: RefCell<HashMap<usize, usize>>,

    // GIL-free regions: events met there wait in ``pending``; the gate is set while one runs
    pub pending: RefCell<Vec<Pend>>,
    pub bus_quiet: Cell<bool>,
    pub gate: Arc<Gate>,
    pub regions: Cell<RegionCounts>,

    // GIL offers: this cycle's least time between two (None: none), the last one's ``perf``
    // time, and whether this cycle made one
    pub offer_s: Cell<Option<f64>>,
    pub last_offer: Cell<f64>,
    pub offered: Cell<bool>,
}

/// A lane object the core met, and its native state when the machine adopted it.
pub type KnownLane = (Obj, Option<Py<PyLane>>);

/// An event met while the GIL was released, emitted (if wanted) when the region ends.
pub enum Pend {
    Stmt {
        kind: &'static str,
        inf: InfRef,
    },
    Strand {
        kind: &'static str,
        job: u64,
        strand: u64,
        addr: i64,
        cont: Option<u64>,
    },
}

impl Core {
    pub fn new(machine: &Bound<'_, PyAny>, seed: i64) -> PyResult<Core> {
        let py = machine.py();
        let h = Host::new(machine, seed)?;
        let clock = py.import("time")?.getattr("monotonic")?.unbind();
        Ok(Core {
            h,
            code: RefCell::new(Code::default()),
            clock: RefCell::new(clock),
            epoch: Instant::now(),
            jobs: RefCell::new(IndexMap::new()),
            keys: PySet::empty(py)?.unbind(),
            ready: RefCell::new(Vec::new()),
            dirty: RefCell::new(IndexSet::new()),
            last_started: RefCell::new(None),
            blocked: RefCell::new(HashMap::new()),
            pool_bytes: Cell::new(0),
            freed: Rc::new(RefCell::new(VecDeque::new())),
            warned_keys: Cell::new(false),
            owned: RefCell::new(HashMap::new()),
            owned_live: Cell::new(0),
            shell: RefCell::new(None),
            inbox: RefCell::new(None),
            entries: RefCell::new(HashMap::new()),
            lanes: RefCell::new(HashMap::new()),
            stats: RefCell::new(Stats::default()),
            inline: RefCell::new(IndexMap::new()),
            inline_at: RefCell::new(HashMap::new()),
            pending: RefCell::new(Vec::new()),
            bus_quiet: Cell::new(false),
            gate: Arc::new(Gate::default()),
            regions: Cell::new(RegionCounts::default()),
            offer_s: Cell::new(None),
            last_offer: Cell::new(0.0),
            offered: Cell::new(false),
        })
    }

    /// Return once no GIL-free region runs (a thread other than the dispatcher's reads the core
    /// only outside one).
    pub fn outside_region(&self, py: Python<'_>) {
        self.gate.wait(py);
    }

    /// Seconds on the core's own monotonic clock (durations and timings).
    pub fn perf(&self) -> f64 {
        self.epoch.elapsed().as_secs_f64()
    }

    /// The scheduling clock (seconds, the lanes' clock).
    pub fn now(&self, py: Python<'_>) -> PyResult<f64> {
        let c = self.clock.borrow().clone_ref(py);
        crate::lanes::lane::as_f64(&c.bind(py).call0()?)
    }

    pub fn job(&self, id: u64) -> Option<JobRef> {
        self.jobs.borrow().get(&id).cloned()
    }

    /// The native state of an adopted lane for calling the methods of ``mask`` directly; None
    /// for a lane run in Python or one whose instance replaces one of them.
    pub fn native_for(
        &self,
        py: Python<'_>,
        lane: &Bound<'_, PyAny>,
        mask: u32,
    ) -> PyResult<Option<Py<PyLane>>> {
        Ok(self
            .native_lane(py, lane)?
            .filter(|n| n.get().c().overridden.get() & mask == 0))
    }

    /// The native state of a lane the machine adopted, or None for a lane run in Python; the
    /// lane learns the core's region gate.
    pub fn native_lane(
        &self,
        py: Python<'_>,
        lane: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Py<PyLane>>> {
        let key = lane.as_ptr() as usize;
        if let Some((o, n)) = self.lanes.borrow().get(&key) {
            if o.bind(py).is(lane) {
                return Ok(n.as_ref().map(|n| n.clone_ref(py)));
            }
        }
        let n = match lane.getattr_opt(pyo3::intern!(py, "_native"))? {
            Some(n) => n.cast_into::<PyLane>().ok().map(|n| n.unbind()),
            None => None,
        };
        if let Some(n) = &n {
            let _ = n.get().c().gate.set(self.gate.clone());
        }
        self.lanes
            .borrow_mut()
            .insert(key, (lane.clone().unbind(), n.as_ref().map(|n| n.clone_ref(py))));
        Ok(n)
    }

    pub fn mark_dirty(&self, id: u64) {
        self.dirty.borrow_mut().insert(id);
    }

    pub fn count_region(&self, f: impl FnOnce(&mut RegionCounts)) {
        let mut r = self.regions.get();
        f(&mut r);
        self.regions.set(r);
    }

    pub fn count_extra(&self, key: &'static str) -> i64 {
        let mut s = self.stats.borrow_mut();
        let n = s.extra.entry(key).or_insert(0);
        *n += 1;
        *n
    }

    /// The counters as the Python core's ``stats`` dict.
    pub fn stats_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        // Copied out first: building the dict may run the collector
        let (ints, busy, max, weak, phase, hist, extra) = {
            let s = self.stats.borrow();
            (
                [
                    s.jobs_ok,
                    s.jobs_failed,
                    s.calls,
                    s.squashed,
                    s.reused,
                    s.wasted,
                    s.store_hits,
                    s.store_misses,
                    s.store_weak,
                    s.store_errors,
                    s.snapshot_errors,
                    s.machine_errors,
                    s.records_blocked,
                    s.records_dropped,
                    s.cycles,
                ],
                s.busy_s,
                s.cycle_max_s,
                s.weak_digests.clone(),
                s.phase_s.clone(),
                s.cycle_hist.clone(),
                s.extra.clone(),
            )
        };
        let inline: Vec<(String, Vec<f64>)> = self
            .inline
            .borrow()
            .iter()
            .map(|(k, st)| (k.clone(), st.times.iter().copied().collect()))
            .collect();
        let d = PyDict::new(py);
        let names = [
            "jobs_ok",
            "jobs_failed",
            "calls",
            "squashed",
            "reused",
            "wasted",
        ];
        for (k, v) in names.iter().zip(&ints[..6]) {
            d.set_item(*k, v)?;
        }
        let il = PyDict::new(py);
        for (node, times) in inline {
            il.set_item(node, times)?;
        }
        d.set_item("inline_s", il)?;
        let names = [
            "store_hits",
            "store_misses",
            "store_weak",
            "store_errors",
            "snapshot_errors",
        ];
        for (k, v) in names.iter().zip(&ints[6..11]) {
            d.set_item(*k, v)?;
        }
        let w = PyDict::new(py);
        for (k, v) in weak {
            w.set_item(k, v)?;
        }
        d.set_item("weak_digests", w)?;
        let names = [
            "machine_errors",
            "records_blocked",
            "records_dropped",
            "cycles",
        ];
        for (k, v) in names.iter().zip(&ints[11..15]) {
            d.set_item(*k, v)?;
        }
        d.set_item("busy_s", busy)?;
        let p = PyDict::new(py);
        for (k, v) in phase {
            p.set_item(k, v)?;
        }
        d.set_item("phase_s", p)?;
        let hh = PyDict::new(py);
        for (k, v) in hist {
            hh.set_item(k, v)?;
        }
        d.set_item("cycle_hist", hh)?;
        d.set_item("cycle_max_s", max)?;
        for (k, v) in extra {
            d.set_item(k, v)?;
        }
        Ok(d)
    }
}
