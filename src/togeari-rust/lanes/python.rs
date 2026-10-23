//! Bindings of the native lane: `togeari._rs.Lane` (the state and logic a
//! `togeari.core.lanes.native.NativeLane` delegates to), the `done` callback engines answer
//! through, and the live bucket views formers written in Python read.

use std::cell::{Cell, OnceCell, RefCell};
use std::collections::{BTreeMap, BinaryHeap, HashMap};
use std::sync::Mutex;

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use super::lane::{
    cap_f64, method_bit, opt_f64, ptr, Config, LaneCore, LaneState, LaneStats, QSel, ResultClasses,
};
use super::queue::{Num, Order, Queue};
use crate::runtime::builtins::Builtins;
use crate::runtime::state::Obj;

/// The lane core, owned by its lane. Only the dispatcher thread runs it; another thread reads
/// it with the GIL held and outside any GIL-free region (``wait``).
struct Shared(LaneCore);

unsafe impl Send for Shared {}
unsafe impl Sync for Shared {}

#[pyclass(name = "Lane", module = "togeari._rs", frozen, weakref)]
pub struct PyLane {
    core: Shared,
}

impl PyLane {
    pub fn c(&self) -> &LaneCore {
        &self.core.0
    }
}

/// What an engine calls when a submission settles: ``done(ticket, results)``.
#[pyclass(module = "togeari._rs", frozen)]
pub struct Done {
    lane: Mutex<Option<Obj>>,
    sub: u64,
}

impl Done {
    pub fn new(lane: Obj, sub: u64) -> Done {
        Done { lane: Mutex::new(Some(lane)), sub }
    }
}

#[pymethods]
impl Done {
    #[allow(unused_variables)]
    fn __call__(
        &self,
        py: Python<'_>,
        ticket: &Bound<'_, PyAny>,
        results: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let lane = self
            .lane
            .lock()
            .expect("the done lock")
            .as_ref()
            .map(|l| l.clone_ref(py));
        let Some(lane) = lane else {
            return Ok(());
        };
        let lane = lane.bind(py);
        lane.getattr(intern!(py, "on_done"))?
            .call1((lane, self.sub, results))?;
        Ok(())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(l) = self.lane.try_lock() {
            if let Some(l) = &*l {
                visit.call(l)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        if let Ok(mut l) = self.lane.try_lock() {
            l.take();
        }
    }
}

/// A bucket of a native lane's queue as a former written in Python reads it, live: ``len``,
/// ``due``, ``cap``, ``key``, ``head()`` and ``items()``.
#[pyclass(module = "togeari._rs", frozen)]
pub struct Bucket {
    lane: Py<PyLane>,
    sel: QSel,
    id: u64,
}

impl Bucket {
    pub fn new(lane: Py<PyLane>, sel: QSel, id: u64) -> Bucket {
        Bucket { lane, sel, id }
    }

    fn with<T>(&self, py: Python<'_>, f: impl FnOnce(Option<&mut Queue>) -> T) -> PyResult<T> {
        let c = self.lane.get().c();
        c.wait(py);
        let mut st =
            c.st.try_borrow_mut()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("the lane is busy"))?;
        Ok(f(st.queue(self.sel).map(|(q, _)| q)))
    }
}

/// The head entry of a bucket: its ``order``, ``item``, ``live`` and ``bucket``.
#[pyclass(module = "togeari._rs", frozen)]
pub struct Head {
    #[pyo3(get)]
    order: Obj,
    #[pyo3(get)]
    item: Obj,
    #[pyo3(get)]
    live: bool,
    #[pyo3(get)]
    bucket: Obj,
}

fn order_py<'py>(py: Python<'py>, o: &Order) -> PyResult<Bound<'py, PyTuple>> {
    let first: Bound<'py, PyAny> = match &o.0 {
        Num::I(i) => i.into_pyobject(py)?.into_any(),
        Num::F(f) => f.into_pyobject(py)?.into_any(),
        Num::B(b) => {
            let int = py.get_type::<pyo3::types::PyInt>();
            let mag =
                int.call_method1(intern!(py, "from_bytes"), (PyBytes::new(py, &b.mag), "big"))?;
            match b.neg {
                true => mag.neg()?,
                false => mag,
            }
        }
    };
    PyTuple::new(py, [first, o.1.into_pyobject(py)?.into_any()])
}

#[pymethods]
impl Bucket {
    fn __len__(&self, py: Python<'_>) -> PyResult<usize> {
        self.with(py, |q| q.map(|q| q.bucket_len(self.id)).unwrap_or(0))
    }

    #[getter]
    fn due(&self, py: Python<'_>) -> PyResult<Option<f64>> {
        self.with(py, |q| q.and_then(|q| q.due(self.id)))
    }

    #[getter]
    fn cap(&self, py: Python<'_>) -> PyResult<Obj> {
        let c = self.lane.get().c();
        let has = self.with(py, |q| q.is_some())?;
        match has {
            true => Ok(c.st.borrow().cap_obj.clone_ref(py)),
            false => Ok(1i64.into_pyobject(py)?.into_any().unbind()),
        }
    }

    #[getter]
    fn key(&self, py: Python<'_>) -> PyResult<Obj> {
        let k = self.with(py, |q| q.and_then(|q| q.bucket_key(py, self.id)))?;
        Ok(k.unwrap_or_else(|| py.None()))
    }

    fn head(slf: &Bound<'_, Self>) -> PyResult<Option<Head>> {
        let py = slf.py();
        let me = slf.get();
        let got = me.with(py, |q| {
            q.and_then(|q| {
                let e = q.head(me.id)?;
                let x = q.entries.get(&e)?;
                Some((x.order.clone(), x.item.clone_ref(py)))
            })
        })?;
        match got {
            Some((order, item)) => Ok(Some(Head {
                order: order_py(py, &order)?.into_any().unbind(),
                item,
                live: true,
                bucket: slf.clone().into_any().unbind(),
            })),
            None => Ok(None),
        }
    }

    fn items<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let items = self.with(py, |q| q.map(|q| q.items(py, self.id)).unwrap_or_default())?;
        PyList::new(py, items.iter().map(|i| i.bind(py)))
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.lane)
    }
}

fn visit_queue(q: &Queue, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
    if let Some(u) = &q.key_unit {
        visit.call(u)?;
    }
    for b in q.buckets.values() {
        visit.call(&b.key)?;
    }
    for e in q.entries.values() {
        visit.call(&e.item)?;
    }
    Ok(())
}

fn visit_state(st: &LaneState, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
    let c = &st.cfg;
    visit.call(&c.select)?;
    if let Some(f) = &c.former {
        visit.call(f)?;
    }
    visit.call(&c.retry)?;
    visit.call(&c.lane_dead_obj)?;
    visit_queue(&st.q, visit)?;
    visit.call(&st.q_keys)?;
    for us in st.state.values() {
        visit.call(&us.unit)?;
        visit.call(&us.keys)?;
        visit_queue(&us.queue, visit)?;
    }
    for i in st.held_items.values() {
        visit.call(i)?;
    }
    for i in st.timeout_items.values() {
        visit.call(i)?;
    }
    for s in st.subs.values() {
        visit.call(&s.unit_obj)?;
        for i in &s.items {
            visit.call(i)?;
        }
        if let Some(t) = &s.ticket {
            visit.call(t)?;
        }
    }
    visit.call(&st.cap_obj)?;
    visit.call(&st.identity)
}

fn submission_cls(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    py.import("togeari.core.lanes.pump")?.getattr("Submission")
}

#[pymethods]
impl PyLane {
    /// The native state of a `togeari.core.lanes.Lane`, read from its configuration; the specs
    /// and engine identity are the lane's own.
    #[new]
    fn new(lane: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = lane.py();
        let g = |n: &str| lane.getattr(n);
        let units = g("units")?.cast_into::<PyList>()?;
        let results = py.import("togeari.core.contracts.results")?;
        let rc = |n: &str| -> PyResult<Obj> { Ok(results.getattr(n)?.unbind()) };
        let former = g("_former")?;
        let lane_dead = g("lane_dead_s")?;
        let cap_obj = g("_q")?.getattr("cap")?;
        let cap = cap_f64(&cap_obj);
        let first = units.iter().next().map(|u| u.unbind());
        let mut specs = HashMap::new();
        for (k, v) in g("_specs")?.cast_into::<PyDict>()?.iter() {
            specs.insert(k.extract::<usize>()?, v.extract::<String>()?);
        }
        let core = LaneCore {
            st: RefCell::new(LaneState {
                cfg: Config {
                    select: g("_select")?.unbind(),
                    former: (!former.is_none()).then(|| former.unbind()),
                    early: g("_forming")?.extract::<String>()? == "early",
                    max_inflight: opt_f64(&g("max_inflight")?)?,
                    retry: g("retry")?.unbind(),
                    timeout_s: opt_f64(&g("timeout_s")?)?,
                    lane_dead_s: opt_f64(&lane_dead)?,
                    lane_dead_obj: lane_dead.unbind(),
                },
                q: Queue::new(first, cap),
                q_keys: PyDict::new(py).unbind(),
                state: IndexMap::new(),
                held: BinaryHeap::new(),
                held_items: BTreeMap::new(),
                held_index: HashMap::new(),
                timeouts: BinaryHeap::new(),
                timeout_items: HashMap::new(),
                seq: 0,
                subs: BTreeMap::new(),
                inflight: 0,
                no_unit_since: None,
                no_unit_counted: 0.0,
                stats: LaneStats::default(),
                batch_sizes: IndexMap::new(),
                waits: Vec::new(),
                up: None,
                up_version: 0,
                pump_needed: false,
                pumped_at: f64::NEG_INFINITY,
                specs,
                identity: g("_identity")?.cast_into::<PyBytes>()?.unbind(),
                cap_obj: cap_obj.unbind(),
            }),
            lane: py
                .import("weakref")?
                .getattr("ref")?
                .call1((lane,))?
                .unbind(),
            me: RefCell::new(None),
            name: RefCell::new(g("name")?.str()?.unbind()),
            units: RefCell::new(units.clone().unbind()),
            clock: RefCell::new(g("clock")?.unbind()),
            builtins: Builtins::new(py)?,
            policy_errors: Cell::new(0),
            overridden: Cell::new(0),
            gate: OnceCell::new(),
            log: py
                .import("logging")?
                .call_method1("getLogger", ("togeari.lane",))?
                .unbind(),
            spec_fn: py
                .import("togeari.core.lanes.lane")?
                .getattr("_spec")?
                .unbind(),
            round: py.import("builtins")?.getattr("round")?.unbind(),
            deadlines: py
                .import("togeari.core.lanes.deadline")?
                .into_any()
                .unbind(),
            select_mod: py
                .import("togeari.core.policies.select")?
                .into_any()
                .unbind(),
            rc: ResultClasses {
                ok: rc("Ok")?,
                item_error: rc("ItemError")?,
                unit_error: rc("UnitError")?,
                cancelled: rc("Cancelled")?,
                unit_unavailable: rc("UnitUnavailable")?,
                item_timeout: rc("ItemTimeout")?,
            },
        };

        // Unit states in the lane's order, each with its own queue
        for u in units.iter() {
            let us = core.unit_state(py, &u, cap);
            core.st.borrow_mut().state.insert(ptr(&u), us);
        }
        Ok(PyLane { core: Shared(core) })
    }

    /// Keep a weak reference to this object (bucket views name it).
    fn _bind(slf: &Bound<'_, Self>) -> PyResult<()> {
        let r = slf.py().import("weakref")?.getattr("ref")?.call1((slf,))?;
        *slf.get().c().me.borrow_mut() = Some(r.unbind());
        Ok(())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let c = self.c();
        if c.gate.get().is_some_and(|g| g.active()) {
            return Ok(());
        }
        visit.call(&c.lane)?;
        if let Ok(m) = c.me.try_borrow() {
            if let Some(m) = &*m {
                visit.call(m)?;
            }
        }
        if let Ok(n) = c.name.try_borrow() {
            visit.call(&*n)?;
        }
        if let Ok(u) = c.units.try_borrow() {
            visit.call(&*u)?;
        }
        if let Ok(clock) = c.clock.try_borrow() {
            visit.call(&*clock)?;
        }
        c.builtins.visit(&visit)?;
        for o in [&c.log, &c.spec_fn, &c.round, &c.select_mod, &c.deadlines] {
            visit.call(o)?;
        }
        let r = &c.rc;
        for o in [
            &r.ok,
            &r.item_error,
            &r.unit_error,
            &r.cancelled,
            &r.unit_unavailable,
            &r.item_timeout,
        ] {
            visit.call(o)?;
        }
        if let Ok(st) = c.st.try_borrow() {
            visit_state(&st, &visit)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let c = self.c();
        if c.gate.get().is_some_and(|g| g.active()) {
            return;
        }
        if let Ok(mut st) = c.st.try_borrow_mut() {
            st.q = Queue::new(None, st.q.cap);
            st.state.clear();
            st.held_items.clear();
            st.held_index.clear();
            st.held.clear();
            st.timeout_items.clear();
            st.timeouts.clear();
            st.subs.clear();
            st.cfg.former = None;
        }
        if let Ok(mut m) = c.me.try_borrow_mut() {
            m.take();
        }
    }

    // ------------------------------------------------------------ the dispatcher interface
    fn enqueue(&self, py: Python<'_>, item: &Bound<'_, PyAny>, now: f64) -> PyResult<()> {
        self.c().wait(py);
        self.c().enqueue(py, item, now)
    }

    fn pump(&self, py: Python<'_>, now: f64) -> PyResult<usize> {
        self.c().wait(py);
        self.c().pump(py, now)
    }

    fn timers<'py>(&self, py: Python<'py>, now: f64) -> PyResult<Bound<'py, PyList>> {
        self.c().wait(py);
        let pairs = self.c().timers(py, now)?;
        PyList::new(py, pairs)
    }

    fn next_deadline(&self, py: Python<'_>, now: f64) -> PyResult<Option<f64>> {
        self.c().wait(py);
        self.c().next_deadline(py, now)
    }

    fn results<'py>(
        &self,
        py: Python<'py>,
        sub: u64,
        results: &Bound<'py, PyAny>,
        now: f64,
    ) -> PyResult<Bound<'py, PyList>> {
        self.c().wait(py);
        let pairs = self.c().results(py, sub, results, now)?;
        PyList::new(py, pairs)
    }

    /// The items and unit of an open submission.
    fn submission<'py>(
        &self,
        py: Python<'py>,
        sub: u64,
    ) -> PyResult<Option<(Bound<'py, PyList>, Obj)>> {
        self.c().wait(py);
        match self.c().sub_items(py, sub) {
            Some((items, unit)) => Ok(Some((PyList::new(py, items)?, unit))),
            None => Ok(None),
        }
    }

    fn remove(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<bool> {
        self.c().wait(py);
        self.c().remove(py, item)
    }

    fn cancel(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<()> {
        self.c().wait(py);
        self.c().cancel(py, item)
    }

    fn attach(&self, py: Python<'_>, unit: &Bound<'_, PyAny>) -> PyResult<()> {
        self.c().wait(py);
        self.c().attach(py, unit)
    }

    fn detach<'py>(
        &self,
        py: Python<'py>,
        unit: &Bound<'py, PyAny>,
        drain: bool,
        now: f64,
    ) -> PyResult<Bound<'py, PyList>> {
        self.c().wait(py);
        let items = self.c().detach(py, unit, drain, now)?;
        PyList::new(py, items)
    }

    /// Take ``unit`` out of service after ``err`` (a ``UnitError``).
    fn unit_down(
        &self,
        py: Python<'_>,
        unit: &Bound<'_, PyAny>,
        err: &Bound<'_, PyAny>,
        now: f64,
    ) -> PyResult<()> {
        self.c().wait(py);
        self.c().unit_down(py, ptr(unit), err, now)
    }

    #[pyo3(signature = (item, now = None))]
    fn requeue_front(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        now: Option<f64>,
    ) -> PyResult<()> {
        self.c().wait(py);
        self.c().requeue_front(py, item, now)
    }

    fn capacity_rose(&self, py: Python<'_>) {
        self.c().wait(py);
        self.c().st.borrow_mut().pump_needed = true;
    }

    // ------------------------------------------------------------ configuration
    /// Queued items are bucketed again under ``former``.
    fn set_former(&self, py: Python<'_>, former: &Bound<'_, PyAny>, now: f64) -> PyResult<()> {
        let c = self.c();
        c.wait(py);
        c.st.borrow_mut().cfg.former = (!former.is_none()).then(|| former.clone().unbind());
        let fm = c.former_now(py)?;
        let sels = c.st.borrow().sels();
        for sel in sels {
            for (o, item) in c.drain(py, sel)? {
                c.push(py, fm.as_ref(), sel, item, o, now)?;
            }
        }
        Ok(())
    }

    fn set_select(&self, py: Python<'_>, select: &Bound<'_, PyAny>) {
        self.c().wait(py);
        self.c().st.borrow_mut().cfg.select = select.clone().unbind();
    }

    fn set_forming(&self, py: Python<'_>, forming: &str) {
        self.c().wait(py);
        self.c().st.borrow_mut().cfg.early = forming == "early";
    }

    fn set_max_inflight(&self, py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<()> {
        self.c().wait(py);
        self.c().st.borrow_mut().cfg.max_inflight = opt_f64(v)?;
        Ok(())
    }

    fn set_timeout_s(&self, py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<()> {
        self.c().wait(py);
        self.c().st.borrow_mut().cfg.timeout_s = opt_f64(v)?;
        Ok(())
    }

    fn set_lane_dead_s(&self, py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<()> {
        self.c().wait(py);
        let d = opt_f64(v)?;
        let mut st = self.c().st.borrow_mut();
        st.cfg.lane_dead_s = d;
        st.cfg.lane_dead_obj = v.clone().unbind();
        Ok(())
    }

    /// The retry policy; its ``max`` and ``backoff_ms`` are read when a retry happens.
    fn set_retry(&self, py: Python<'_>, v: &Bound<'_, PyAny>) {
        self.c().wait(py);
        self.c().st.borrow_mut().cfg.retry = v.clone().unbind();
    }

    fn set_name(&self, py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<()> {
        self.c().wait(py);
        *self.c().name.borrow_mut() = v.str()?.unbind();
        Ok(())
    }

    fn set_units(&self, py: Python<'_>, v: &Bound<'_, PyList>) {
        self.c().wait(py);
        *self.c().units.borrow_mut() = v.clone().unbind();
    }

    /// An instance attribute named ``name`` replaces (or stops replacing) a method the core
    /// calls directly.
    fn set_overridden(&self, py: Python<'_>, name: &str, on: bool) {
        let bit = method_bit(name);
        let c = self.c();
        c.wait(py);
        c.overridden.set(if on {
            c.overridden.get() | bit
        } else {
            c.overridden.get() & !bit
        });
    }

    #[getter]
    fn clock(&self, py: Python<'_>) -> Obj {
        self.c().wait(py);
        self.c().clock.borrow().clone_ref(py)
    }

    #[setter]
    fn set_clock(&self, py: Python<'_>, clock: Obj) {
        self.c().wait(py);
        *self.c().clock.borrow_mut() = clock;
    }

    // ------------------------------------------------------------ views
    fn units_up<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        self.c().wait(py);
        let objs: Vec<Obj> = {
            let mut st = self.c().st.borrow_mut();
            let up = st.units_up();
            up.iter().map(|u| st.state[u].unit.clone_ref(py)).collect()
        };
        PyList::new(py, objs)
    }

    #[getter]
    fn up_version(&self, py: Python<'_>) -> i64 {
        self.c().wait(py);
        self.c().st.borrow().up_version
    }

    fn rate(&self, py: Python<'_>, unit: &Bound<'_, PyAny>) -> PyResult<f64> {
        self.c().wait(py);
        match self.c().st.borrow().state.get(&ptr(unit)) {
            Some(s) => Ok(s.rate),
            None => Err(pyo3::exceptions::PyKeyError::new_err(ptr(unit))),
        }
    }

    fn queued_now(&self, py: Python<'_>) -> usize {
        self.c().wait(py);
        self.c().st.borrow().queued_now()
    }

    fn queued<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.c().wait(py);
        self.c().queued_body(py)?.into_py(py)
    }

    fn load_waiting<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.c().wait(py);
        self.c().load_waiting_body(py)
    }

    /// ``queue``, ``inflight``, ``units_up`` and ``waiting``, each read through the lane's
    /// methods (an instance's own when it set one).
    fn signals<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let c = self.c();
        c.wait(py);
        let d = PyDict::new(py);
        d.set_item("queue", c.v_queued(py)?.into_py(py)?)?;
        d.set_item("inflight", c.st.borrow().inflight)?;
        d.set_item("units_up", c.v_units_up(py)?.len())?;
        d.set_item("waiting", c.v_load_waiting(py)?)?;
        Ok(d)
    }

    fn take_waits(&self, py: Python<'_>) -> Vec<f64> {
        self.c().wait(py);
        std::mem::take(&mut self.c().st.borrow_mut().waits)
    }

    fn under_cap(&self, py: Python<'_>) -> bool {
        self.c().wait(py);
        self.c().st.borrow().under_cap()
    }

    fn engine_identity(&self, py: Python<'_>) -> Py<PyBytes> {
        self.c().wait(py);
        self.c().st.borrow().identity.clone_ref(py)
    }

    /// Every queued item in queue order.
    fn queue<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        self.c().wait(py);
        let mut all = {
            let st = self.c().st.borrow();
            let mut all = st.q.all(py);
            for us in st.state.values() {
                all.extend(us.queue.all(py));
            }
            all
        };
        all.sort_by(|a, b| a.0.cmp(&b.0));
        PyList::new(py, all.into_iter().map(|(_, i)| i))
    }

    /// Each unit's state as ``(unit, state, inflight, subs, rate, busy_since, busy_total,
    /// items_done, errors, next_poll, backoff, submit_lat)``.
    fn unit_rows<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        self.c().wait(py);
        type Row = (
            Obj,
            &'static str,
            i64,
            Vec<u64>,
            f64,
            Option<f64>,
            f64,
            i64,
            Vec<(String, i64)>,
            f64,
            f64,
            f64,
        );
        let rows: Vec<Row> = {
            let st = self.c().st.borrow();
            st.state
                .values()
                .map(|us| {
                    (
                        us.unit.clone_ref(py),
                        us.state.name(),
                        us.inflight,
                        us.subs.iter().copied().collect(),
                        us.rate,
                        us.busy_since,
                        us.busy_total,
                        us.items_done,
                        us.errors.iter().map(|(k, v)| (k.clone(), *v)).collect(),
                        us.next_poll,
                        us.backoff,
                        us.submit_lat,
                    )
                })
                .collect()
        };
        let out = PyList::empty(py);
        for r in rows {
            let subs = PyDict::new(py);
            for s in r.3 {
                subs.set_item(s, py.None())?;
            }
            let errors = PyDict::new(py);
            for (k, v) in r.8 {
                errors.set_item(k, v)?;
            }
            let row = PyTuple::new(
                py,
                [
                    r.0.into_bound(py),
                    PyString::new(py, r.1).into_any(),
                    r.2.into_pyobject(py)?.into_any(),
                    subs.into_any(),
                    r.4.into_pyobject(py)?.into_any(),
                    r.5.into_pyobject(py)?.into_any(),
                    r.6.into_pyobject(py)?.into_any(),
                    r.7.into_pyobject(py)?.into_any(),
                    errors.into_any(),
                    r.9.into_pyobject(py)?.into_any(),
                    r.10.into_pyobject(py)?.into_any(),
                    r.11.into_pyobject(py)?.into_any(),
                ],
            )?;
            out.append(row)?;
        }
        Ok(out)
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.c().wait(py);
        self.c().stats_dict(py)
    }

    fn batch_sizes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.c().wait(py);
        let pairs: Vec<(i64, i64)> = self
            .c()
            .st
            .borrow()
            .batch_sizes
            .iter()
            .map(|(k, v)| (*k, *v))
            .collect();
        let d = PyDict::new(py);
        for (k, v) in pairs {
            d.set_item(k, v)?;
        }
        Ok(d)
    }

    #[getter]
    fn inflight(&self, py: Python<'_>) -> i64 {
        self.c().wait(py);
        self.c().st.borrow().inflight
    }

    /// Open submissions in submit order: id -> ``Submission``.
    fn subs<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.c().wait(py);
        type Row = (u64, Obj, Vec<Obj>, Option<Obj>, f64);
        let rows: Vec<Row> = {
            let st = self.c().st.borrow();
            st.subs
                .iter()
                .map(|(k, s)| {
                    (
                        *k,
                        s.unit_obj.clone_ref(py),
                        s.items.iter().map(|i| i.clone_ref(py)).collect(),
                        s.ticket.as_ref().map(|t| t.clone_ref(py)),
                        s.sent,
                    )
                })
                .collect()
        };
        let cls = submission_cls(py)?;
        let d = PyDict::new(py);
        for (k, unit, items, ticket, sent) in rows {
            let sub = cls.call1((k, unit, PyList::new(py, items)?, ticket, sent, true))?;
            d.set_item(k, sub)?;
        }
        Ok(d)
    }

    fn unit_state_name(&self, py: Python<'_>, unit: &Bound<'_, PyAny>) -> Option<&'static str> {
        self.c().wait(py);
        self.c()
            .st
            .borrow()
            .state
            .get(&ptr(unit))
            .map(|s| s.state.name())
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyLane>()?;
    m.add_class::<Done>()?;
    m.add_class::<Bucket>()?;
    m.add_class::<Head>()?;
    Ok(())
}
