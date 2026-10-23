//! The native lane: queues, unit states, submissions, retries, timeouts and health
//! (`togeari.core.lanes.lane` and `health`). Every call into Python runs between short borrows.

use std::cell::{Cell, OnceCell, RefCell};
use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap};
use std::rc::Rc;
use std::sync::Arc;

use indexmap::IndexMap;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString};

use super::policy::{DeadlineK, DueK, FormerNow, KeyK};
use super::queue::{Ask, Big, Num, Order, Queue, F, FRONT, READY};
use crate::contracts::hash::sha256;
use crate::runtime::builtins::{plain_f64, Builtins, Tag};
use crate::runtime::gate::Gate;
use crate::runtime::host::is_exception;
use crate::runtime::state::Obj;

// Health-poll backoff of a down unit: first wait and ceiling, in seconds
pub const POLL_FIRST_S: f64 = 0.5;
pub const POLL_MAX_S: f64 = 30.0;

/// Dispatcher-interface methods an instance may replace; the core then calls the replacement.
pub const ENQUEUE: u32 = 1;
pub const PUMP: u32 = 2;
pub const TIMERS: u32 = 4;
pub const NEXT_DEADLINE: u32 = 8;
pub const RESULTS: u32 = 16;
pub const CANCEL: u32 = 32;
pub const QUEUED: u32 = 64;
pub const UNITS_UP: u32 = 128;
pub const QUEUED_NOW: u32 = 256;
pub const UNDER_CAP: u32 = 512;
pub const LOAD_WAITING: u32 = 1024;
pub const REMOVE: u32 = 2048;
pub const UNIT_DOWN: u32 = 4096;
pub const REQUEUE_FRONT: u32 = 8192;
pub const RATE: u32 = 16384;

pub fn method_bit(name: &str) -> u32 {
    match name {
        "enqueue" => ENQUEUE,
        "pump" => PUMP,
        "timers" => TIMERS,
        "next_deadline" => NEXT_DEADLINE,
        "results" => RESULTS,
        "cancel" => CANCEL,
        "queued" => QUEUED,
        "units_up" => UNITS_UP,
        "queued_now" => QUEUED_NOW,
        "under_cap" => UNDER_CAP,
        "load_waiting" => LOAD_WAITING,
        "remove" => REMOVE,
        "unit_down" => UNIT_DOWN,
        "requeue_front" => REQUEUE_FRONT,
        "rate" => RATE,
        _ => 0,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UState {
    Up,
    Down,
    Draining,
    Detached,
}

impl UState {
    pub fn name(self) -> &'static str {
        match self {
            UState::Up => "up",
            UState::Down => "down",
            UState::Draining => "draining",
            UState::Detached => "detached",
        }
    }
}

/// One of the lane's queues: the shared one, or a unit's (early forming).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum QSel {
    Shared,
    Unit(usize),
}

pub struct UnitState {
    pub unit: Obj,
    pub queue: Queue,
    // Bucket key -> bucket id of the unit's queue
    pub keys: Py<PyDict>,
    pub state: UState,
    pub inflight: i64,
    pub subs: BTreeSet<u64>,
    // Items per second, EWMA
    pub rate: f64,
    pub busy_since: Option<f64>,
    pub busy_total: f64,
    pub items_done: i64,
    pub errors: IndexMap<String, i64>,
    pub next_poll: f64,
    pub backoff: f64,
    pub submit_lat: f64,
}

pub struct Submission {
    pub unit: usize,
    pub unit_obj: Obj,
    pub items: Vec<Obj>,
    pub ticket: Option<Obj>,
    pub sent: f64,
}

/// A timeout entry: (deadline, seq, the submission or None when armed at enqueue).
pub type Timeout = Reverse<(F, u64, Option<u64>)>;

#[derive(Default)]
pub struct LaneStats {
    pub submitted: i64,
    pub retries: i64,
    pub failovers: i64,
    pub recoveries: i64,
    pub batches: i64,
    pub no_unit_s: f64,
    pub dead_failed: i64,
}

/// The lane's configuration as set on the lane object: the objects, and the numbers read from
/// them (None for a None setting).
pub struct Config {
    pub select: Obj,
    pub former: Option<Obj>,
    pub early: bool,
    pub max_inflight: Option<f64>,
    pub retry: Obj,
    pub timeout_s: Option<f64>,
    pub lane_dead_s: Option<f64>,
    pub lane_dead_obj: Obj,
}

pub struct LaneState {
    pub cfg: Config,

    // Queues: the shared one and one per unit (early forming); unit states by unit id
    pub q: Queue,
    pub q_keys: Py<PyDict>,
    pub state: IndexMap<usize, UnitState>,

    // Retries waiting for their backoff by sequence, item timeouts by sequence
    pub held: BinaryHeap<Reverse<(F, u64)>>,
    pub held_items: BTreeMap<u64, Obj>,
    pub held_index: HashMap<usize, u64>,
    pub timeouts: BinaryHeap<Timeout>,
    pub timeout_items: HashMap<u64, Obj>,
    pub seq: u64,

    // Open submissions by id (submit order), counters
    pub subs: BTreeMap<u64, Submission>,
    pub inflight: i64,
    pub no_unit_since: Option<f64>,
    pub no_unit_counted: f64,
    pub stats: LaneStats,
    pub batch_sizes: IndexMap<i64, i64>,
    pub waits: Vec<f64>,

    // Units-up cache, pump bookkeeping, engine identity
    pub up: Option<Vec<usize>>,
    pub up_version: i64,
    pub pump_needed: bool,
    pub pumped_at: f64,
    pub specs: HashMap<usize, String>,
    pub identity: Py<PyBytes>,

    // The batch cap of every queue as Python's ``_cap()`` gives it (``Bucket.cap`` hands it out)
    pub cap_obj: Obj,
}

impl LaneState {
    pub fn next_seq(&mut self) -> u64 {
        let s = self.seq;
        self.seq += 1;
        s
    }

    pub fn units_up(&mut self) -> Vec<usize> {
        if self.up.is_none() {
            self.up = Some(
                self.state
                    .iter()
                    .filter(|(_, s)| s.state == UState::Up)
                    .map(|(k, _)| *k)
                    .collect(),
            );
        }
        self.up.clone().expect("units up")
    }

    pub fn queued_now(&self) -> usize {
        self.q.len() + self.state.values().map(|s| s.queue.len()).sum::<usize>()
    }

    pub fn queued(&self) -> usize {
        self.queued_now() + self.held_items.len()
    }

    pub fn under_cap(&self) -> bool {
        self.cfg
            .max_inflight
            .is_none_or(|m| (self.inflight as f64) < m)
    }

    pub fn set_state(&mut self, u: usize, s: UState) {
        if let Some(st) = self.state.get_mut(&u) {
            st.state = s;
        }
        self.up = None;
        self.up_version += 1;
        self.pump_needed = true;
    }

    pub fn queue(&mut self, sel: QSel) -> Option<(&mut Queue, &Py<PyDict>)> {
        match sel {
            QSel::Shared => Some((&mut self.q, &self.q_keys)),
            QSel::Unit(u) => self.state.get_mut(&u).map(|s| (&mut s.queue, &s.keys)),
        }
    }

    pub fn sels(&self) -> Vec<QSel> {
        let mut v = vec![QSel::Shared];
        v.extend(self.state.keys().map(|u| QSel::Unit(*u)));
        v
    }

    /// Whether the item is queued or held.
    pub fn waiting(&self, p: usize) -> bool {
        self.held_index.contains_key(&p)
            || self.q.contains(p)
            || self.state.values().any(|u| u.queue.contains(p))
    }
}

pub struct ResultClasses {
    pub ok: Obj,
    pub item_error: Obj,
    pub unit_error: Obj,
    pub cancelled: Obj,
    pub unit_unavailable: Obj,
    pub item_timeout: Obj,
}

/// The native lane: its state, the lane object it backs (weakly), its policy errors.
pub struct LaneCore {
    pub st: RefCell<LaneState>,
    pub lane: Obj,
    pub me: RefCell<Option<Obj>>,
    pub name: RefCell<Py<PyString>>,
    pub units: RefCell<Py<PyList>>,
    pub clock: RefCell<Obj>,
    pub builtins: Builtins,
    pub policy_errors: Cell<i64>,
    pub overridden: Cell<u32>,
    pub gate: OnceCell<Arc<Gate>>,
    pub log: Obj,
    pub spec_fn: Obj,
    pub round: Obj,
    pub select_mod: Obj,
    pub deadlines: Obj,
    pub resources: RefCell<Option<Obj>>,
    pub resource_helpers: Obj,
    pub rc: ResultClasses,
}

/// A count as the lane's own body gives it, or as a replacing method returned it.
pub enum Count<'py> {
    N(usize),
    Py(Bound<'py, PyAny>),
}

impl<'py> Count<'py> {
    pub fn truthy(&self) -> PyResult<bool> {
        match self {
            Count::N(n) => Ok(*n > 0),
            Count::Py(o) => o.is_truthy(),
        }
    }

    pub fn into_py(self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Count::N(n) => Ok(n.into_pyobject(py)?.into_any()),
            Count::Py(o) => Ok(o),
        }
    }
}

pub fn ptr(o: &Bound<'_, PyAny>) -> usize {
    o.as_ptr() as usize
}

/// A plain number, or ``float(v)`` (a TypeError for a non-number, as Python's comparisons give).
/// Python's ``min(a, b)``: ``a`` unless ``b`` is less (ties and NaN comparisons keep ``a``).
pub fn py_min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}

/// Python's ``max(a, b)``: ``a`` unless ``b`` is greater.
pub fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// SizeOrWait's ``len(bucket) >= max(1, min(size, cap))``.
pub fn sow_full(n: usize, size: f64, cap: f64) -> bool {
    n as f64 >= py_max(1.0, py_min(size, cap))
}

/// A batch cap as an f64 for the queues' full tests: an int past the float range is infinite.
pub fn cap_f64(v: &Bound<'_, PyAny>) -> f64 {
    as_f64(v).unwrap_or(f64::INFINITY)
}

pub fn as_f64(v: &Bound<'_, PyAny>) -> PyResult<f64> {
    match plain_f64(v) {
        Some(x) => Ok(x),
        None => v
            .py()
            .get_type::<pyo3::types::PyFloat>()
            .call1((v,))?
            .extract(),
    }
}

/// ``None`` or a number.
pub fn opt_f64(v: &Bound<'_, PyAny>) -> PyResult<Option<f64>> {
    if v.is_none() {
        Ok(None)
    } else {
        as_f64(v).map(Some)
    }
}

/// An order key's first part from a priority: ``-priority``, exact for ints of any size.
pub fn neg_priority(p: &Bound<'_, PyAny>) -> PyResult<Num> {
    if let Ok(i) = p.extract::<i128>() {
        if let Some(n) = i.checked_neg() {
            return Ok(Num::I(n));
        }
    }
    if p.is_instance_of::<pyo3::types::PyInt>() {
        return big_num(&p.neg()?);
    }
    if let Some(f) = plain_f64(p) {
        return Ok(Num::F(-f));
    }
    let n = p.neg()?;
    if n.is_instance_of::<pyo3::types::PyInt>() {
        return big_num(&n);
    }
    Ok(Num::F(as_f64(&n)?))
}

/// A Python int as a ``Num``: an i128 when it fits, else its sign and magnitude.
pub fn big_num(n: &Bound<'_, PyAny>) -> PyResult<Num> {
    if let Ok(i) = n.extract::<i128>() {
        return Ok(Num::I(i));
    }
    let py = n.py();
    let neg = n.lt(0)?;
    let mag = n.call_method0(intern!(py, "__abs__"))?;
    let bits: usize = mag.call_method0(intern!(py, "bit_length"))?.extract()?;
    let bytes = mag
        .call_method1(intern!(py, "to_bytes"), (bits.div_ceil(8), "big"))?
        .extract::<Vec<u8>>()?;
    Ok(Num::B(Rc::new(Big { neg, mag: bytes })))
}

impl LaneCore {
    pub fn lane<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.lane.bind(py).call0()
    }

    pub fn now(&self, py: Python<'_>) -> PyResult<f64> {
        let c = self.clock.borrow().clone_ref(py);
        as_f64(&c.bind(py).call0()?)
    }

    /// Return once no GIL-free region of the core this lane serves runs.
    pub fn wait(&self, py: Python<'_>) {
        if let Some(g) = self.gate.get() {
            g.wait(py);
        }
    }

    /// A lane policy raised: counted in the lane's ``policy_errors``, logged at the 1st, 2nd,
    /// 4th, 8th ... error.
    pub fn policy_failed(&self, py: Python<'_>, what: &str, e: &PyErr) {
        let n = self.policy_errors.get() + 1;
        self.policy_errors.set(n);
        if n & (n - 1) == 0 {
            let name = self.name.borrow().clone_ref(py);
            let msg = format!("lane {}: {what} failed ({n} so far); fallback used", name.bind(py));
            let kw = PyDict::new(py);
            let _ = kw.set_item("exc_info", e.value(py));
            let _ = self.log.bind(py).call_method("error", (msg,), Some(&kw));
        }
    }

    /// ``r`` or, when the policy raised an Exception, ``fallback`` (counted).
    pub fn contained<T>(
        &self,
        py: Python<'_>,
        what: &str,
        r: PyResult<T>,
        fallback: T,
    ) -> PyResult<T> {
        match r {
            Ok(v) => Ok(v),
            Err(e) if is_exception(py, &e) => {
                self.policy_failed(py, what, &e);
                Ok(fallback)
            }
            Err(e) => Err(e),
        }
    }

    pub fn emit(
        &self,
        py: Python<'_>,
        kind: &str,
        fields: &[(&str, Bound<'_, PyAny>)],
    ) -> PyResult<()> {
        let lane = self.lane(py)?;
        let kw = PyDict::new(py);
        for (k, v) in fields {
            kw.set_item(*k, v)?;
        }
        lane.getattr(intern!(py, "emit"))?
            .call((kind,), Some(&kw))?;
        Ok(())
    }

    /// The unit object of a unit id.
    pub fn unit_obj(&self, py: Python<'_>, u: usize) -> Option<Obj> {
        self.st.borrow().state.get(&u).map(|s| s.unit.clone_ref(py))
    }

    // ------------------------------------------------------------ methods an instance may replace
    // Each is called where the Python lane calls ``self.<method>``: the instance's own when it set
    // one (``overridden``), else the native body.

    fn replaced(&self, bit: u32) -> bool {
        self.overridden.get() & bit != 0
    }

    /// ``self.units_up()`` as unit ids.
    pub fn v_units_up(&self, py: Python<'_>) -> PyResult<Vec<usize>> {
        if !self.replaced(UNITS_UP) {
            return Ok(self.st.borrow_mut().units_up());
        }
        let got = self.lane(py)?.call_method0(intern!(py, "units_up"))?;
        let mut out = Vec::new();
        for u in got.try_iter()? {
            let p = ptr(&u?);
            if !self.st.borrow().state.contains_key(&p) {
                return Err(pyo3::exceptions::PyKeyError::new_err(p));
            }
            out.push(p);
        }
        Ok(out)
    }

    /// ``self.queued_now()``.
    pub fn v_queued_now<'py>(&self, py: Python<'py>) -> PyResult<Count<'py>> {
        if !self.replaced(QUEUED_NOW) {
            return Ok(Count::N(self.st.borrow().queued_now()));
        }
        Ok(Count::Py(self.lane(py)?.call_method0(intern!(py, "queued_now"))?))
    }

    /// ``self.queued_now() + len(held)``, the body of ``queued``.
    pub fn queued_body<'py>(&self, py: Python<'py>) -> PyResult<Count<'py>> {
        let held = self.st.borrow().held_items.len();
        Ok(match self.v_queued_now(py)? {
            Count::N(n) => Count::N(n + held),
            Count::Py(o) => Count::Py(o.add(held)?),
        })
    }

    /// ``self.queued()``.
    pub fn v_queued<'py>(&self, py: Python<'py>) -> PyResult<Count<'py>> {
        if !self.replaced(QUEUED) {
            return self.queued_body(py);
        }
        Ok(Count::Py(self.lane(py)?.call_method0(intern!(py, "queued"))?))
    }

    /// ``self.under_cap()``.
    pub fn v_under_cap(&self, py: Python<'_>) -> PyResult<bool> {
        if !self.replaced(UNDER_CAP) {
            return Ok(self.st.borrow().under_cap());
        }
        self.lane(py)?
            .call_method0(intern!(py, "under_cap"))?
            .is_truthy()
    }

    /// The body of ``load_waiting``: ``int(load().waiting)`` summed over ``self.units_up()``.
    pub fn load_waiting_body<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let up = self.v_units_up(py)?;
        let int = py.get_type::<pyo3::types::PyInt>();
        let mut total = 0i64.into_pyobject(py)?.into_any();
        for u in up {
            let Some(u) = self.unit_obj(py, u) else {
                continue;
            };
            let w = u
                .bind(py)
                .call_method0(intern!(py, "load"))
                .and_then(|l| l.getattr(intern!(py, "waiting")))
                .and_then(|w| int.call1((w,)));
            match w {
                Ok(w) => total = total.add(w)?,
                Err(e) if is_exception(py, &e) => {}
                Err(e) => return Err(e),
            }
        }
        Ok(total)
    }

    /// ``self.load_waiting()``.
    pub fn v_load_waiting<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if !self.replaced(LOAD_WAITING) {
            return self.load_waiting_body(py);
        }
        self.lane(py)?.call_method0(intern!(py, "load_waiting"))
    }

    /// ``self.remove(item)``.
    pub fn v_remove(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<bool> {
        if !self.replaced(REMOVE) {
            return self.remove(py, item);
        }
        self.lane(py)?
            .call_method1(intern!(py, "remove"), (item,))?
            .is_truthy()
    }

    /// ``self.unit_down(unit, err, now)``.
    pub fn v_unit_down(
        &self,
        py: Python<'_>,
        unit: usize,
        err: &Bound<'_, PyAny>,
        now: f64,
    ) -> PyResult<()> {
        if !self.replaced(UNIT_DOWN) {
            return self.unit_down(py, unit, err, now);
        }
        let u = self.unit_obj(py, unit).unwrap_or_else(|| py.None());
        self.lane(py)?
            .call_method1(intern!(py, "unit_down"), (u, err, now))?;
        Ok(())
    }

    /// ``self.requeue_front(item, now)``.
    pub fn v_requeue_front(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        now: f64,
    ) -> PyResult<()> {
        if !self.replaced(REQUEUE_FRONT) {
            return self.requeue_front(py, item, Some(now));
        }
        self.lane(py)?
            .call_method1(intern!(py, "requeue_front"), (item, now))?;
        Ok(())
    }

    /// ``self.results(sub, results, now)`` (a submission named by its id).
    pub fn v_results(
        &self,
        py: Python<'_>,
        sid: u64,
        results: &Bound<'_, PyAny>,
        now: f64,
    ) -> PyResult<Vec<(Obj, Obj)>> {
        if !self.replaced(RESULTS) {
            return self.results(py, sid, results, now);
        }
        let got = self
            .lane(py)?
            .call_method1(intern!(py, "results"), (sid, results, now))?;
        let mut out = Vec::new();
        for pair in got.try_iter()? {
            let pair = pair?;
            out.push((pair.get_item(0)?.unbind(), pair.get_item(1)?.unbind()));
        }
        Ok(out)
    }

    /// ``-lane.rate(unit)`` as the Throughput selector's key part.
    pub fn v_neg_rate<'py>(&self, py: Python<'py>, unit: usize) -> PyResult<Bound<'py, PyAny>> {
        if !self.replaced(RATE) {
            let r = self
                .st
                .borrow()
                .state
                .get(&unit)
                .map(|s| s.rate)
                .unwrap_or(0.0);
            return Ok((-r).into_pyobject(py)?.into_any());
        }
        let u = self.unit_obj(py, unit).unwrap_or_else(|| py.None());
        self.lane(py)?
            .call_method1(intern!(py, "rate"), (u,))?
            .neg()
    }

    pub fn former_now(&self, py: Python<'_>) -> PyResult<Option<FormerNow>> {
        let f = self
            .st
            .borrow()
            .cfg
            .former
            .as_ref()
            .map(|f| f.clone_ref(py));
        match f {
            Some(f) => Ok(Some(FormerNow::of(py, &self.builtins, f.bind(py))?)),
            None => Ok(None),
        }
    }

    // ------------------------------------------------------------ former asks (no borrow held)
    /// The contract's default key: the unit's own key, None when it has none or raises.
    fn default_key(&self, py: Python<'_>, item: &Obj, key_unit: Option<&Obj>) -> PyResult<Obj> {
        let Some(u) = key_unit else {
            return Ok(py.None());
        };
        let u = u.bind(py);
        if self.builtins.method(py, u, "key")? == Some(Tag::EngineKey) {
            return Ok(py.None());
        }
        match u.call_method1(intern!(py, "key"), (item.bind(py),)) {
            Ok(k) => Ok(k.unbind()),
            Err(e) if is_exception(py, &e) => Ok(py.None()),
            Err(e) => Err(e),
        }
    }

    fn key_with(
        &self,
        py: Python<'_>,
        k: &KeyK,
        item: &Obj,
        key_unit: Option<&Obj>,
    ) -> PyResult<Obj> {
        match k {
            KeyK::Default => self.default_key(py, item, key_unit),
            KeyK::Bucketed { inner, key_fn } => {
                let a = self.key_with(py, inner, item, key_unit)?;
                let b = key_fn.bind(py).call1((item.bind(py),))?;
                Ok(pyo3::types::PyTuple::new(py, [a.bind(py), &b])?
                    .into_any()
                    .unbind())
            }
            KeyK::User(f) => {
                let u = key_unit
                    .map(|u| u.bind(py).clone())
                    .unwrap_or_else(|| py.None().into_bound(py));
                f.bind(py)
                    .call_method1(intern!(py, "key"), (item.bind(py), u))
                    .map(|k| k.unbind())
            }
        }
    }

    /// The bucket key of ``item``; None when the former raises.
    pub fn key_of(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        item: &Obj,
        key_unit: Option<&Obj>,
    ) -> PyResult<Obj> {
        let Some(fm) = fm else {
            return Ok(py.None());
        };
        let r = self.key_with(py, &fm.key, item, key_unit);
        self.contained(py, "former key", r, py.None())
    }

    /// The due time of ``item``; None when the former raises.
    pub fn due_of(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        item: &Obj,
    ) -> PyResult<Option<f64>> {
        let Some(fm) = fm else {
            return Ok(None);
        };
        let r: PyResult<Option<Obj>> = match &fm.due {
            DueK::Never => return Ok(None),
            DueK::Sow { wait } => {
                let r = sow_due(py, *wait, item);
                match self.contained(py, "former due", r.map(Some), None)? {
                    Some(d) => return Ok(Some(d)),
                    None => return Ok(None),
                }
            }
            DueK::User(f) => f
                .bind(py)
                .call_method1(intern!(py, "due"), (item.bind(py),))
                .map(|d| Some(d.unbind())),
        };
        match self.contained(py, "former due", r, None)? {
            Some(d) => opt_f64(d.bind(py)),
            None => Ok(None),
        }
    }

    // ------------------------------------------------------------ queue operations
    /// Queue ``item`` at ``order`` in queue ``sel``.
    pub fn push(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        sel: QSel,
        item: Obj,
        order: Order,
        now: f64,
    ) -> PyResult<()> {
        if self.resources.borrow().is_some() {
            item.bind(py)
                .setattr("_resource_order", super::python::order_py(py, &order)?)?;
        }
        let got = {
            let mut st = self.st.borrow_mut();
            st.queue(sel)
                .map(|(q, keys)| (q.key_unit.as_ref().map(|u| u.clone_ref(py)), keys.clone_ref(py)))
        };
        let Some((key_unit, keys)) = got else {
            return Ok(());
        };
        let key = self.key_of(py, fm, &item, key_unit.as_ref())?;
        let found: Option<u64> = match keys.bind(py).get_item(key.bind(py))? {
            Some(b) => Some(b.extract()?),
            None => None,
        };
        let due = self.due_of(py, fm, &item)?;
        let p = item.as_ptr() as usize;
        let placed = {
            let mut st = self.st.borrow_mut();
            st.queue(sel)
                .map(|(q, _)| q.insert(py, item, p, order, found, &key, due))
        };
        let Some((b, new)) = placed else {
            return Ok(());
        };
        if new {
            keys.bind(py).set_item(key.bind(py), b)?;
        }
        self.refresh(py, fm, sel, b, now)
    }

    /// Recompute a bucket's deadline; an empty bucket leaves its queue.
    pub fn refresh(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        sel: QSel,
        b: u64,
        now: f64,
    ) -> PyResult<()> {
        let got = {
            let mut st = self.st.borrow_mut();
            st.queue(sel)
                .map(|(q, keys)| (q.ask(py, b), keys.clone_ref(py)))
        };
        let Some((ask, keys)) = got else {
            return Ok(());
        };
        let d = match ask {
            Ask::Gone => return Ok(()),
            Ask::Emptied(key) => {
                let keys = keys.bind(py);
                let mine = match keys.get_item(key.bind(py))? {
                    Some(v) => v.extract::<u64>().ok() == Some(b),
                    None => false,
                };
                if mine {
                    keys.del_item(key.bind(py))?;
                }
                return Ok(());
            }
            Ask::Input { n, due, cap } => match fm.map(|f| &f.deadline) {
                None | Some(DeadlineK::Ready) => (n > 0).then_some(READY),
                Some(DeadlineK::Sow { size }) => {
                    if sow_full(n, *size, cap) {
                        Some(READY)
                    } else {
                        due
                    }
                }
                Some(DeadlineK::User(f)) => {
                    let view = self.view(py, sel, b)?;
                    let r = f
                        .bind(py)
                        .call_method1(intern!(py, "deadline"), (view, now))
                        .map(|d| d.unbind());
                    let d = self.contained(py, "former deadline", r.map(Some), None)?;
                    match d {
                        None => Some(READY),
                        Some(d) => opt_f64(d.bind(py))?,
                    }
                }
            },
        };
        if let Some((q, _)) = self.st.borrow_mut().queue(sel) {
            q.apply(b, d, now);
        }
        Ok(())
    }

    /// A live view of bucket ``b`` of queue ``sel`` for a former written in Python.
    pub fn view(&self, py: Python<'_>, sel: QSel, b: u64) -> PyResult<Obj> {
        let me = self
            .me
            .borrow()
            .as_ref()
            .map(|m| m.clone_ref(py))
            .expect("a bound native lane");
        let lane = me.bind(py).call0()?;
        let lane = lane.cast_into::<super::python::PyLane>()?.unbind();
        Ok(Py::new(py, super::python::Bucket::new(lane, sel, b))?.into_any())
    }

    /// Drop an item from queue ``sel``; True if it was there.
    pub fn remove_from(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        sel: QSel,
        p: usize,
        now: f64,
    ) -> PyResult<bool> {
        let b = {
            let mut st = self.st.borrow_mut();
            st.queue(sel).and_then(|(q, _)| q.remove(p))
        };
        match b {
            Some(b) => {
                self.refresh(py, fm, sel, b, now)?;
                Ok(true)
            }
            None => Ok(false),
        }
    }

    /// Every queued ``(order, item)`` of queue ``sel`` in queue order; the queue is left empty.
    pub fn drain(&self, py: Python<'_>, sel: QSel) -> PyResult<Vec<(Order, Obj)>> {
        let got = {
            let mut st = self.st.borrow_mut();
            st.queue(sel)
                .map(|(q, keys)| (q.drain(py), keys.clone_ref(py)))
        };
        match got {
            Some((items, keys)) => {
                keys.bind(py).clear();
                Ok(items)
            }
            None => Ok(Vec::new()),
        }
    }

    /// Queue ``sel`` takes batch cap ``cap``; its buckets are refreshed in creation order.
    pub fn set_cap(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        sel: QSel,
        cap: f64,
        now: f64,
    ) -> PyResult<()> {
        let ids = {
            let mut st = self.st.borrow_mut();
            match st.queue(sel) {
                Some((q, _)) => {
                    q.cap = cap;
                    q.bucket_ids()
                }
                None => return Ok(()),
            }
        };
        for b in ids {
            self.refresh(py, fm, sel, b, now)?;
        }
        Ok(())
    }

    /// Buckets of queue ``sel`` whose deadline passed become ripe.
    pub fn promote(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        sel: QSel,
        now: f64,
    ) -> PyResult<()> {
        loop {
            let b = {
                let mut st = self.st.borrow_mut();
                st.queue(sel).and_then(|(q, _)| q.promote_next(now))
            };
            let Some(b) = b else { return Ok(()) };
            self.refresh(py, fm, sel, b, now)?;
        }
    }

    pub fn resource_prepare(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        phase: &str,
    ) -> PyResult<bool> {
        let resource = self.resources.borrow().as_ref().map(|r| r.clone_ref(py));
        match resource {
            None => Ok(true),
            Some(r) => r
                .bind(py)
                .call_method1("prepare", (item, phase))?
                .is_truthy(),
        }
    }

    pub fn resource_move(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        phase: Option<&str>,
    ) -> PyResult<()> {
        let resource = self.resources.borrow().as_ref().map(|r| r.clone_ref(py));
        if let Some(r) = resource {
            r.bind(py).call_method1("move", (item, phase))?;
        }
        Ok(())
    }

    // ------------------------------------------------------------ queueing
    /// Queue ``item``; a ``WorkItem.deadline`` it carries also bounds its time queued.
    pub fn enqueue(&self, py: Python<'_>, item: &Bound<'_, PyAny>, now: f64) -> PyResult<()> {
        if !self.resource_prepare(py, item, "queued")? {
            return Ok(());
        }
        let ext = item.getattr(intern!(py, "ext"))?.cast_into::<PyDict>()?;
        if !ext.contains("retries")? {
            ext.set_item("retries", 0)?;
        }
        ext.set_item("enqueued", now)?;
        if !ext.contains("deadline_cap")? {
            let deadline = item.getattr(intern!(py, "deadline"))?;
            ext.set_item("deadline_cap", &deadline)?;
            if !deadline.is_none() {
                let d = as_f64(&deadline)?;
                let mut st = self.st.borrow_mut();
                let s = st.next_seq();
                st.timeouts.push(Reverse((F(d), s, None)));
                st.timeout_items.insert(s, item.clone().unbind());
            }
        }
        let neg = neg_priority(&item.getattr(intern!(py, "priority"))?)?;
        let s = self.st.borrow_mut().next_seq();
        let fm = self.former_now(py)?;
        self.route(py, fm.as_ref(), item.clone().unbind(), (neg, s as i64), now)
    }

    /// Drop a queued or held item (squashed); True if it was there.
    pub fn remove(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<bool> {
        self.resource_move(py, item, None)?;
        let now = self.now(py)?;
        let p = ptr(item);
        let held = {
            let mut st = self.st.borrow_mut();
            match st.held_index.remove(&p) {
                Some(s) => st.held_items.remove(&s),
                None => None,
            }
        };
        if held.is_some() {
            return Ok(true);
        }
        let (sels, queued) = {
            let st = self.st.borrow();
            (st.sels(), st.waiting(p))
        };
        if !queued {
            return Ok(false);
        }
        let fm = self.former_now(py)?;
        for sel in sels {
            if self.remove_from(py, fm.as_ref(), sel, p, now)? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// A squashed item: drop it from the queue, or cancel its submission when all its items
    /// are squashed.
    pub fn cancel(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<()> {
        let ext = item.getattr(intern!(py, "ext"))?;
        ext.set_item("squashed", true)?;
        if self.v_remove(py, item)? {
            return Ok(());
        }
        let sid = ext.call_method1(intern!(py, "get"), ("sub",))?;
        let Ok(sid) = sid.extract::<u64>() else {
            return Ok(());
        };
        let got = {
            let st = self.st.borrow();
            st.subs.get(&sid).map(|sub| {
                (
                    sub.items
                        .iter()
                        .map(|i| i.clone_ref(py))
                        .collect::<Vec<_>>(),
                    sub.unit_obj.clone_ref(py),
                    sub.ticket.as_ref().map(|t| t.clone_ref(py)),
                )
            })
        };
        let Some((items, unit, ticket)) = got else {
            return Ok(());
        };
        for it in &items {
            if !flag(py, it, "squashed")? {
                return Ok(());
            }
        }
        self.cancel_ticket(py, &unit, ticket)
    }

    /// ``unit.cancel(ticket)``; an Exception it raises is dropped.
    pub fn cancel_ticket(&self, py: Python<'_>, unit: &Obj, ticket: Option<Obj>) -> PyResult<()> {
        let t = ticket.unwrap_or_else(|| py.None());
        match unit.bind(py).call_method1(intern!(py, "cancel"), (t,)) {
            Ok(_) => Ok(()),
            Err(e) if is_exception(py, &e) => Ok(()),
            Err(e) => Err(e),
        }
    }

    // ------------------------------------------------------------ results
    /// Settle a submission's results once: ``(item, result)`` pairs to deliver. ``UnitError``
    /// re-queues items (after the retry backoff) within ``retry.max`` and takes the unit down.
    pub fn results(
        &self,
        py: Python<'_>,
        sid: u64,
        results: &Bound<'_, PyAny>,
        now: f64,
    ) -> PyResult<Vec<(Obj, Obj)>> {
        if !self.st.borrow().subs.contains_key(&sid) {
            return Ok(Vec::new());
        }
        let (unit, items, unit_obj) = self.close(sid, now);
        for item in &items {
            self.resource_move(py, item.bind(py), None)?;
        }
        self.st.borrow_mut().pump_needed = true;

        // A result count that differs from the item count fails every item
        let mut results: Vec<Bound<'_, PyAny>> =
            results.try_iter()?.collect::<PyResult<Vec<_>>>()?;
        if results.len() != items.len() {
            let name = unit_obj.bind(py).getattr(intern!(py, "name"))?;
            let msg = format!(
                "engine {} returned {} results for {} items",
                name.repr()?,
                results.len(),
                items.len()
            );
            let err = pyo3::exceptions::PyRuntimeError::new_err(msg);
            let r = self.rc.item_error.bind(py).call1((err.value(py),))?;
            results = vec![r; items.len()];
        }

        // Per item: deliver, or retry after a unit error
        let rc = &self.rc;
        let mut out = Vec::new();
        let mut retry = Vec::new();
        let mut unit_error: Option<Bound<'_, PyAny>> = None;
        for (it, r) in items.iter().zip(results.iter()) {
            let ext = it.bind(py).getattr(intern!(py, "ext"))?;
            if r.is_instance(rc.unit_error.bind(py))? {
                unit_error = Some(r.clone());
            }
            if truthy_get(&ext, "settled")? {
                continue;
            }
            let enforced = self
                .deadlines
                .bind(py)
                .call_method1("expire", (it.bind(py), now, true))?;
            let r = if enforced.is_none() { r } else { &enforced };
            if r.is_instance(rc.unit_error.bind(py))? {
                unit_error = Some(r.clone());
                if truthy_get(&ext, "squashed")? {
                    continue;
                }
                let spent = truthy_get(&ext, "ordered_once")? || {
                    let retry = self.st.borrow().cfg.retry.clone_ref(py);
                    let max = retry.bind(py).getattr(intern!(py, "max"))?;
                    ext.get_item("retries")?.ge(&max)?
                };
                if spent {
                    ext.set_item("settled", true)?;
                    let uname = unit_obj.bind(py).getattr(intern!(py, "name"))?;
                    let err = rc.unit_unavailable.bind(py).call1((format!(
                        "{}: {}",
                        uname.str()?,
                        r.getattr(intern!(py, "error"))?.str()?
                    ),))?;
                    out.push((it.clone_ref(py), rc.item_error.bind(py).call1((err,))?.unbind()));
                } else {
                    retry.push(it.clone_ref(py));
                }
                continue;
            }
            if r.is_instance(rc.ok.bind(py))? {
                let sent = as_f64(&ext.call_method1(intern!(py, "get"), ("sent", now))?)?;
                let dt = (now - sent).max(1e-6);
                if let Some(us) = self.st.borrow_mut().state.get_mut(&unit) {
                    us.items_done += 1;
                    us.rate = if us.rate != 0.0 {
                        0.8 * us.rate + 0.2 / dt
                    } else {
                        1.0 / dt
                    };
                }
            } else if r.is_instance(rc.item_error.bind(py))? {
                let k = r
                    .getattr(intern!(py, "error"))?
                    .get_type()
                    .name()?
                    .to_string();
                if let Some(us) = self.st.borrow_mut().state.get_mut(&unit) {
                    *us.errors.entry(k).or_insert(0) += 1;
                }
            }
            ext.set_item("settled", true)?;
            out.push((it.clone_ref(py), r.clone().unbind()));
        }

        // Unit down first, so retried items go to other units
        if let Some(err) = unit_error {
            self.v_unit_down(py, unit, &err, now)?;
        }
        for it in retry {
            let ext = it.bind(py).getattr(intern!(py, "ext"))?;
            let n = ext.get_item("retries")?.add(1)?;
            ext.set_item("retries", n)?;
            self.st.borrow_mut().stats.retries += 1;
            self.hold(py, it, now)?;
        }
        self.check_drained(py, unit, now)?;
        Ok(out)
    }

    // ------------------------------------------------------------ time
    /// The earliest time the lane has something to do; a past time only when an action is
    /// possible now.
    pub fn next_deadline(&self, py: Python<'_>, now: f64) -> PyResult<Option<f64>> {
        let resource = self.resources.borrow().as_ref().map(|r| r.clone_ref(py));
        if let Some(r) = resource {
            if r.bind(py).getattr("pending")?.is_truthy()? {
                return Ok(Some(now));
            }
        }
        let mut ds: Vec<f64> = Vec::new();
        {
            let mut st = self.st.borrow_mut();

            // Health polls
            ds.extend(
                st.state
                    .values()
                    .filter(|s| s.state == UState::Down)
                    .map(|s| s.next_poll),
            );

            // Retries waiting for their backoff
            while let Some(Reverse((_, s))) = st.held.peek() {
                if st.held_items.contains_key(s) {
                    break;
                }
                st.held.pop();
            }
            if let Some(Reverse((t, _))) = st.held.peek() {
                ds.push(t.0);
            }
        }

        // Item timeouts
        loop {
            let top = self.st.borrow().timeouts.peek().copied();
            let Some(Reverse((t, s, sub))) = top else {
                break;
            };
            if self.timing(py, s, sub)? {
                ds.push(t.0);
                break;
            }
            let mut st = self.st.borrow_mut();
            if st.timeouts.peek().is_some_and(|x| x.0 .1 == s) {
                st.timeouts.pop();
            }
            st.timeout_items.remove(&s);
        }

        // Queued work
        if self.v_queued_now(py)?.truthy()? {
            let up = self.v_units_up(py)?;
            if up.is_empty() {
                let st = self.st.borrow();
                if let (Some(dead), Some(since)) = (st.cfg.lane_dead_s, st.no_unit_since) {
                    ds.push(since + dead);
                }
            } else {
                {
                    let mut st = self.st.borrow_mut();
                    if st.pump_needed {
                        ds.push(now);
                    }
                    for sel in st.sels() {
                        if let Some(t) = st.queue(sel).and_then(|(q, _)| q.next_time()) {
                            ds.push(t);
                        }
                    }
                }
                ds.extend(self.wake_times(py, &up)?);
            }
        }
        Ok(ds.into_iter().reduce(f64::min))
    }

    /// Attempt timers require an open submission; the original call cap stays active
    /// through queueing, attempts and backoff until settlement or squash.
    pub fn timing(&self, py: Python<'_>, seq: u64, sub: Option<u64>) -> PyResult<bool> {
        let item = match self.st.borrow().timeout_items.get(&seq) {
            Some(i) => i.clone_ref(py),
            None => return Ok(false),
        };
        let item = self
            .deadlines
            .bind(py)
            .call_method1("current", (item,))?
            .unbind();
        if flag(py, &item, "settled")? || flag(py, &item, "squashed")? {
            return Ok(false);
        }
        let st = self.st.borrow();
        Ok(match sub {
            Some(s) => st.subs.contains_key(&s),
            None => true, // The initial call deadline survives every attempt/backoff.
        })
    }

    /// ``wake_at()`` of units up that have queued work, when past the last pump.
    fn wake_times(&self, py: Python<'_>, up: &[usize]) -> PyResult<Vec<f64>> {
        let mut out = Vec::new();
        let (shared, pumped_at, units): (bool, f64, Vec<(Obj, bool)>) = {
            let st = self.st.borrow();
            let units = up
                .iter()
                .filter_map(|u| st.state.get(u))
                .map(|s| (s.unit.clone_ref(py), s.queue.len() > 0))
                .collect();
            (st.q.len() > 0, st.pumped_at, units)
        };
        for (unit, own) in units {
            let Some(wake_at) = unit.bind(py).getattr_opt(intern!(py, "wake_at"))? else {
                continue;
            };
            if wake_at.is_none() || !(shared || own) {
                continue;
            }
            let t = match wake_at.call0() {
                Ok(t) => t,
                Err(e) if is_exception(py, &e) => continue,
                Err(e) => return Err(e),
            };
            if !t.is_none() && t.gt(pumped_at)? {
                out.push(as_f64(&t)?);
            }
        }
        Ok(out)
    }

    // ------------------------------------------------------------ health
    /// Take ``unit`` out of service after a ``UnitError``; its queued items go to the shared
    /// queue.
    pub fn unit_down(
        &self,
        py: Python<'_>,
        unit: usize,
        err: &Bound<'_, PyAny>,
        now: f64,
    ) -> PyResult<()> {
        let (backoff, unit_obj) = {
            let st = self.st.borrow();
            let Some(us) = st.state.get(&unit) else {
                return Err(pyo3::exceptions::PyKeyError::new_err(unit));
            };
            if us.state != UState::Up {
                return Ok(());
            }
            (us.backoff, us.unit.clone_ref(py))
        };
        self.st.borrow_mut().set_state(unit, UState::Down);

        // Next health poll: the engine's Retry-After, else the backoff
        let ra = err.getattr_opt(intern!(py, "retry_after"))?;
        let delay = match ra {
            Some(r) if r.is_truthy()? => as_f64(&r)?,
            _ => backoff,
        };
        let error = err.getattr_opt(intern!(py, "error"))?;
        let k = match &error {
            Some(e) => e.get_type().name()?.to_string(),
            None => "UnitError".to_string(),
        };
        {
            let mut st = self.st.borrow_mut();
            let us = st.state.get_mut(&unit).expect("a unit");
            us.next_poll = now + delay;
            us.backoff = POLL_MAX_S.min(us.backoff * 2.0);
            *us.errors.entry(k).or_insert(0) += 1;
            st.stats.failovers += 1;
        }
        self.move_to_shared(py, unit, now)?;
        let reason = match error {
            Some(e) => e.str()?,
            None => err.str()?,
        };
        let name = unit_obj.bind(py).getattr(intern!(py, "name"))?;
        self.emit(py, "unit.down", &[("unit", name), ("reason", reason.into_any())])
    }

    fn poll(&self, py: Python<'_>, unit: usize, now: f64) -> PyResult<()> {
        let Some(u) = self.unit_obj(py, unit) else {
            return Ok(());
        };
        let ok = match u
            .bind(py)
            .call_method0(intern!(py, "ready"))
            .and_then(|v| v.is_truthy())
        {
            Ok(v) => v,
            Err(e) if is_exception(py, &e) => false,
            Err(e) => return Err(e),
        };
        if !ok {
            let mut st = self.st.borrow_mut();
            let us = st.state.get_mut(&unit).expect("a unit");
            us.next_poll = now + us.backoff;
            us.backoff = POLL_MAX_S.min(us.backoff * 2.0);
            return Ok(());
        }
        {
            let mut st = self.st.borrow_mut();
            st.set_state(unit, UState::Up);
            st.state.get_mut(&unit).expect("a unit").backoff = POLL_FIRST_S;
            st.stats.recoveries += 1;
        }
        let name = u.bind(py).getattr(intern!(py, "name"))?;
        self.emit(
            py,
            "unit.up",
            &[
                ("unit", name),
                ("reason", PyString::new(py, "ready").into_any()),
            ],
        )
    }

    pub fn move_to_shared(&self, py: Python<'_>, unit: usize, now: f64) -> PyResult<()> {
        let items = self.drain(py, QSel::Unit(unit))?;
        if items.is_empty() {
            return Ok(());
        }
        let fm = self.former_now(py)?;
        for (order, item) in items {
            self.push(py, fm.as_ref(), QSel::Shared, item, order, now)?;
        }
        Ok(())
    }

    pub fn check_drained(&self, py: Python<'_>, unit: usize, now: f64) -> PyResult<()> {
        let draining = self
            .st
            .borrow()
            .state
            .get(&unit)
            .is_some_and(|s| s.state == UState::Draining && s.inflight == 0);
        if draining {
            self.detached(py, unit, now, "drained")?;
        }
        Ok(())
    }

    /// The unit is detached: it leaves ``units`` and ``unit.detached`` is emitted.
    pub fn detached(&self, py: Python<'_>, unit: usize, now: f64, reason: &str) -> PyResult<()> {
        self.st.borrow_mut().set_state(unit, UState::Detached);
        self.leave(py, unit, now)?;
        let Some(u) = self.unit_obj(py, unit) else {
            return Ok(());
        };
        let name = u.bind(py).getattr(intern!(py, "name"))?;
        self.emit(
            py,
            "unit.detached",
            &[
                ("unit", name),
                ("reason", PyString::new(py, reason).into_any()),
            ],
        )
    }

    // ------------------------------------------------------------ elastic units
    pub fn unit_state(&self, py: Python<'_>, unit: &Bound<'_, PyAny>, cap: f64) -> UnitState {
        UnitState {
            unit: unit.clone().unbind(),
            queue: Queue::new(Some(unit.clone().unbind()), cap),
            keys: PyDict::new(py).unbind(),
            state: UState::Up,
            inflight: 0,
            subs: BTreeSet::new(),
            rate: 0.0,
            busy_since: None,
            busy_total: 0.0,
            items_done: 0,
            errors: IndexMap::new(),
            next_poll: 0.0,
            backoff: POLL_FIRST_S,
            submit_lat: 0.0,
        }
    }

    /// The smallest ``max(1, max_batch or 1)`` among the lane's units, through Python's
    /// ``min``/``max`` (the number as Python gives it, and as an f64 for the queues).
    pub fn cap_of<'py>(&self, py: Python<'py>) -> PyResult<(f64, Bound<'py, PyAny>)> {
        let units: Vec<Bound<'_, PyAny>> = self.units.borrow().bind(py).iter().collect();
        let b = py.import(intern!(py, "builtins"))?;
        let (max, min) = (b.getattr(intern!(py, "max"))?, b.getattr(intern!(py, "min"))?);
        let one = 1i64.into_pyobject(py)?.into_any();
        let mut caps = Vec::with_capacity(units.len());
        for u in units {
            let mb = match u.getattr_opt(intern!(py, "max_batch"))? {
                Some(v) if v.is_truthy()? => v,
                _ => one.clone(),
            };
            caps.push(max.call1((&one, mb))?);
        }
        let kw = PyDict::new(py);
        kw.set_item(intern!(py, "default"), &one)?;
        let cap = min.call((PyList::new(py, caps)?,), Some(&kw))?;
        Ok((cap_f64(&cap), cap))
    }

    pub fn digest_specs(&self, py: Python<'_>) -> Py<PyBytes> {
        let units: Vec<usize> = self
            .units
            .borrow()
            .bind(py)
            .iter()
            .map(|u| ptr(&u))
            .collect();
        let st = self.st.borrow();
        let mut specs: Vec<&String> = units.iter().filter_map(|u| st.specs.get(u)).collect();
        specs.sort();
        specs.dedup();
        let joined = specs
            .iter()
            .map(|s| s.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        PyBytes::new(py, &sha256(joined.as_bytes())).unbind()
    }

    /// ``unit`` joins ``units``: its spec is read, and the batch cap, the shared queue's key
    /// unit and the engine identity follow.
    fn join(&self, py: Python<'_>, unit: &Bound<'_, PyAny>, now: f64) -> PyResult<usize> {
        self.units.borrow().bind(py).append(unit)?;
        self.lane(py)?
            .call_method1(intern!(py, "_listen"), (unit,))?;
        let key = ptr(unit);
        let spec: String = self.spec_fn.bind(py).call1((unit,))?.extract()?;
        self.st.borrow_mut().specs.insert(key, spec);
        if !self.st.borrow().state.contains_key(&key) {
            let cap = self.st.borrow().q.cap;
            let us = self.unit_state(py, unit, cap);
            self.st.borrow_mut().state.insert(key, us);
        }
        self.units_changed(py, now)?;
        Ok(key)
    }

    /// A detached unit leaves ``units``; its state stays for views and a later attach.
    fn leave(&self, py: Python<'_>, unit: usize, now: f64) -> PyResult<()> {
        let list = self.units.borrow().clone_ref(py);
        let list = list.bind(py);
        if let Some(i) = list.iter().position(|u| ptr(&u) == unit) {
            list.del_item(i)?;
        }
        self.units_changed(py, now)
    }

    fn units_changed(&self, py: Python<'_>, now: f64) -> PyResult<()> {
        let (cap, obj) = self.cap_of(py)?;

        // Every queue holds the same cap: Python's ``q.cap != cap`` is one comparison
        let old = self.st.borrow().cap_obj.clone_ref(py);
        if old.bind(py).ne(&obj)? {
            self.st.borrow_mut().cap_obj = obj.unbind();
            let sels = self.st.borrow().sels();
            let fm = self.former_now(py)?;
            for sel in sels {
                self.set_cap(py, fm.as_ref(), sel, cap, now)?;
            }
        }
        let units: Vec<Bound<'_, PyAny>> = self.units.borrow().bind(py).iter().collect();
        {
            let mut st = self.st.borrow_mut();
            let in_units =
                st.q.key_unit
                    .as_ref()
                    .is_some_and(|k| units.iter().any(|u| u.is(k)));
            if !in_units {
                st.q.key_unit = units.first().map(|u| u.clone().unbind());
            }
        }
        let id = self.digest_specs(py);
        self.st.borrow_mut().identity = id;
        Ok(())
    }

    /// Add a started ``unit``, or bring a detached or draining one back.
    pub fn attach(&self, py: Python<'_>, unit: &Bound<'_, PyAny>) -> PyResult<()> {
        let now = self.now(py)?;
        let key = ptr(unit);
        let name = unit.getattr(intern!(py, "name"))?;
        let state = self.st.borrow().state.get(&key).map(|s| s.state);
        let lane_name = self.name.borrow().clone_ref(py);
        match state {
            None => {
                let units: Vec<Bound<'_, PyAny>> = self.units.borrow().bind(py).iter().collect();
                for u in units {
                    if u.getattr(intern!(py, "name"))?.eq(&name)? {
                        let msg = format!(
                            "lane {} has a unit named {}",
                            lane_name.bind(py).repr()?,
                            name.repr()?
                        );
                        return Err(pyo3::exceptions::PyValueError::new_err(msg));
                    }
                }
                let known: Vec<(usize, Obj)> = {
                    let st = self.st.borrow();
                    st.state
                        .iter()
                        .map(|(k, s)| (*k, s.unit.clone_ref(py)))
                        .collect()
                };
                for (k, u) in known {
                    if u.bind(py).getattr(intern!(py, "name"))?.eq(&name)? {
                        self.st.borrow_mut().state.shift_remove(&k);
                    }
                }
                self.join(py, unit, now)?;
            }
            Some(UState::Detached) => {
                self.join(py, unit, now)?;
            }
            Some(UState::Draining) => {}
            Some(_) => {
                let msg = format!(
                    "unit {} is already in lane {}",
                    name.repr()?,
                    lane_name.bind(py).repr()?
                );
                return Err(pyo3::exceptions::PyValueError::new_err(msg));
            }
        }
        if let Some(us) = self.st.borrow_mut().state.get_mut(&key) {
            us.backoff = POLL_FIRST_S;
        }

        // Up when ready, down and polled otherwise
        let ok = match unit
            .call_method0(intern!(py, "ready"))
            .and_then(|v| v.is_truthy())
        {
            Ok(v) => v,
            Err(e) if is_exception(py, &e) => false,
            Err(e) => return Err(e),
        };
        {
            let mut st = self.st.borrow_mut();
            st.set_state(key, if ok { UState::Up } else { UState::Down });
            if let Some(us) = st.state.get_mut(&key) {
                us.next_poll = now + us.backoff;
            }
        }
        self.emit(
            py,
            "unit.attach",
            &[
                ("unit", name),
                ("reason", PyString::new(py, "attach").into_any()),
            ],
        )
    }

    /// Stop giving ``unit`` work; returns the items re-queued.
    pub fn detach(
        &self,
        py: Python<'_>,
        unit: &Bound<'_, PyAny>,
        drain: bool,
        now: f64,
    ) -> PyResult<Vec<Obj>> {
        let key = ptr(unit);
        match self.st.borrow().state.get(&key).map(|s| s.state) {
            None | Some(UState::Detached) => return Ok(Vec::new()),
            _ => {}
        }
        self.move_to_shared(py, key, now)?;
        if drain {
            self.st.borrow_mut().set_state(key, UState::Draining);
            let name = unit.getattr(intern!(py, "name"))?;
            self.emit(
                py,
                "unit.detach",
                &[
                    ("unit", name),
                    ("reason", PyString::new(py, "drain").into_any()),
                ],
            )?;
            self.check_drained(py, key, now)?;
            return Ok(Vec::new());
        }

        // Cancel every open submission in submit order and re-queue its unsettled items; a
        // submission holding an ordered item finishes on this unit
        let mut requeued = Vec::new();
        let mut kept = false;
        let sids: Vec<u64> = self.st.borrow().state[&key].subs.iter().copied().collect();
        for sid in sids {
            let got = {
                let st = self.st.borrow();
                st.subs.get(&sid).map(|sub| {
                    (
                        sub.items
                            .iter()
                            .map(|i| i.clone_ref(py))
                            .collect::<Vec<_>>(),
                        sub.ticket.as_ref().map(|t| t.clone_ref(py)),
                    )
                })
            };
            let Some((items, ticket)) = got else { continue };
            let mut ordered = false;
            for it in &items {
                if flag(py, it, "ordered_once")? {
                    ordered = true;
                    break;
                }
            }
            if ordered {
                kept = true;
                continue;
            }
            self.cancel_ticket(py, &unit.clone().unbind(), ticket)?;
            self.close(sid, now);
            for it in items {
                if !flag(py, &it, "settled")? && !flag(py, &it, "squashed")? {
                    self.v_requeue_front(py, it.bind(py), now)?;
                    requeued.push(it);
                }
            }
        }

        // Under early forming a re-queued item may have gone to this unit's own queue
        self.move_to_shared(py, key, now)?;
        let name = unit.getattr(intern!(py, "name"))?;
        self.emit(
            py,
            "unit.detach",
            &[
                ("unit", name),
                ("reason", PyString::new(py, "now").into_any()),
            ],
        )?;
        if kept {
            self.st.borrow_mut().set_state(key, UState::Draining);
            self.check_drained(py, key, now)?;
        } else {
            self.detached(py, key, now, "now")?;
        }
        Ok(requeued)
    }

    // ------------------------------------------------------------ timers
    /// Health polls, retries past their backoff, item timeouts and the dead-lane limit; the
    /// ``(item, result)`` pairs to deliver.
    pub fn timers(&self, py: Python<'_>, now: f64) -> PyResult<Vec<(Obj, Obj)>> {
        let resource = self.resources.borrow().as_ref().map(|r| r.clone_ref(py));
        let mut out: Vec<(Obj, Obj)> = match resource {
            Some(r) => r.bind(py).call_method0("drain")?.extract()?,
            None => Vec::new(),
        };

        // Health polls of down units
        let polls: Vec<usize> = {
            let st = self.st.borrow();
            st.state
                .iter()
                .filter(|(_, s)| s.state == UState::Down && now >= s.next_poll)
                .map(|(k, _)| *k)
                .collect()
        };
        for u in polls {
            self.poll(py, u, now)?;
        }

        // Retries whose backoff ended go to the front of the queue
        loop {
            let item = {
                let mut st = self.st.borrow_mut();
                match st.held.peek() {
                    Some(Reverse((t, _))) if t.0 <= now => {}
                    _ => break,
                }
                let Reverse((_, s)) = st.held.pop().expect("a held entry");
                let item = st.held_items.remove(&s);
                if let Some(i) = &item {
                    st.held_index.remove(&(i.as_ptr() as usize));
                }
                item
            };
            if let Some(item) = item {
                self.v_requeue_front(py, item.bind(py), now)?;
            }
        }

        // Item timeouts: a waiting item leaves the lane; one in flight is settled, and a
        // submission whose items all settled is cancelled
        let rc = &self.rc;
        let mut expired: Vec<u64> = Vec::new();
        loop {
            let top = {
                let st = self.st.borrow();
                match st.timeouts.peek() {
                    Some(Reverse((t, s, sub))) if t.0 <= now => Some((*s, *sub)),
                    _ => None,
                }
            };
            let Some((s, sub)) = top else { break };
            let timing = self.timing(py, s, sub)?;
            let item = {
                let mut st = self.st.borrow_mut();
                if st.timeouts.peek().is_some_and(|x| x.0 .1 == s) {
                    st.timeouts.pop();
                }
                st.timeout_items.remove(&s)
            };
            let (true, Some(item)) = (timing, item) else {
                continue;
            };
            let item = self
                .deadlines
                .bind(py)
                .call_method1("current", (item,))?
                .unbind();
            let itb = item.bind(py);
            let ext = itb.getattr(intern!(py, "ext"))?;
            let current = match sub {
                Some(sid) => Some(sid),
                None => ext
                    .call_method1(intern!(py, "get"), ("sub",))?
                    .extract::<Option<u64>>()?
                    .filter(|sid| self.st.borrow().subs.contains_key(sid)),
            };
            let result = self
                .deadlines
                .bind(py)
                .call_method1("expire", (itb, now, current.is_some()))?;
            if result.is_none() {
                continue;
            }
            ext.set_item("settled", true)?;
            self.resource_move(py, itb, None)?;
            out.push((item.clone_ref(py), result.unbind()));
            if let Some(sid) = current {
                expired.push(sid);
            } else {
                self.v_remove(py, itb)?;
            }
        }
        for sid in expired {
            let got = {
                let st = self.st.borrow();
                st.subs.get(&sid).map(|sub| {
                    (
                        sub.items
                            .iter()
                            .map(|i| i.clone_ref(py))
                            .collect::<Vec<_>>(),
                        sub.unit_obj.clone_ref(py),
                        sub.ticket.as_ref().map(|t| t.clone_ref(py)),
                    )
                })
            };
            let Some((items, unit, ticket)) = got else {
                continue;
            };
            let mut all = true;
            for it in &items {
                if !flag(py, it, "settled")? {
                    all = false;
                    break;
                }
            }
            if all {
                self.cancel_ticket(py, &unit, ticket)?;
                let cancelled = rc.cancelled.bind(py).call0()?;
                let res = PyList::new(py, std::iter::repeat_n(cancelled, items.len()))?;
                out.extend(self.v_results(py, sid, res.as_any(), now)?);
            }
        }

        // Dead lane: queued and held items fail
        if self.dead(now) {
            let failed = self.fail_queued(py)?;
            if !failed.is_empty() {
                let dead = {
                    let mut st = self.st.borrow_mut();
                    st.stats.dead_failed += failed.len() as i64;
                    st.cfg.lane_dead_obj.clone_ref(py)
                };
                let name = self.name.borrow().clone_ref(py);
                self.emit(
                    py,
                    "lane.dead",
                    &[
                        ("lane", name.bind(py).clone().into_any()),
                        ("items", failed.len().into_pyobject(py)?.into_any()),
                    ],
                )?;
                let err = rc.unit_unavailable.bind(py).call1((format!(
                    "lane {}: no unit up for {}s",
                    name.bind(py),
                    dead.bind(py).str()?
                ),))?;
                for it in failed {
                    let r = rc.item_error.bind(py).call1((&err,))?;
                    out.push((it, r.unbind()));
                }
            }
        }
        Ok(out)
    }

    /// The same sum ``next_deadline`` reports, so the clock reaching it makes the lane dead.
    fn dead(&self, now: f64) -> bool {
        let st = self.st.borrow();
        matches!((st.cfg.lane_dead_s, st.no_unit_since), (Some(d), Some(s)) if now >= s + d)
    }

    fn fail_queued(&self, py: Python<'_>) -> PyResult<Vec<Obj>> {
        let mut failed: Vec<Obj> = Vec::new();
        let sels = self.st.borrow().sels();
        for sel in sels {
            failed.extend(self.drain(py, sel)?.into_iter().map(|(_, i)| i));
        }
        {
            let mut st = self.st.borrow_mut();
            failed.extend(std::mem::take(&mut st.held_items).into_values());
            st.held_index.clear();
            st.held.clear();
        }
        for it in &failed {
            self.resource_move(py, it.bind(py), None)?;
            it.bind(py)
                .getattr(intern!(py, "ext"))?
                .set_item("settled", true)?;
        }
        Ok(failed)
    }

    /// Re-queue a retried item after the retry backoff.
    fn hold(&self, py: Python<'_>, item: Obj, now: f64) -> PyResult<()> {
        if !self.resource_prepare(py, item.bind(py), "retry_held")? {
            return Ok(());
        }
        let retry = self.st.borrow().cfg.retry.clone_ref(py);
        let ms = retry.bind(py).getattr(intern!(py, "backoff_ms"))?;
        let wait = as_f64(&ms)? / 1000.0;
        if wait <= 0.0 {
            return self.v_requeue_front(py, item.bind(py), now);
        }
        let mut st = self.st.borrow_mut();
        let s = st.next_seq();
        st.held_index.insert(item.as_ptr() as usize, s);
        st.held_items.insert(s, item);
        st.held.push(Reverse((F(now + wait), s)));
        Ok(())
    }

    /// Put ``item`` back ahead of every queued item.
    pub fn requeue_front(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        now: Option<f64>,
    ) -> PyResult<()> {
        let now = match now {
            Some(n) => n,
            None => self.now(py)?,
        };
        if !self.resource_prepare(py, item, "queued")? {
            return Ok(());
        }
        let s = self.st.borrow_mut().next_seq() as i64;
        let fm = self.former_now(py)?;
        self.route(py, fm.as_ref(), item.clone().unbind(), (Num::I(FRONT), -s), now)
    }

    // ------------------------------------------------------------ views
    pub fn stats_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let s = {
            let st = self.st.borrow();
            let s = &st.stats;
            (
                s.submitted,
                s.retries,
                s.failovers,
                s.recoveries,
                s.batches,
                s.no_unit_s,
                s.dead_failed,
            )
        };
        let d = PyDict::new(py);
        d.set_item("submitted", s.0)?;
        d.set_item("retries", s.1)?;
        d.set_item("failovers", s.2)?;
        d.set_item("recoveries", s.3)?;
        d.set_item("batches", s.4)?;
        d.set_item("no_unit_s", s.5)?;
        d.set_item("dead_failed", s.6)?;
        if self.policy_errors.get() > 0 {
            d.set_item("policy_errors", self.policy_errors.get())?;
        }
        Ok(d)
    }
}

/// The SizeOrWait due time: ``ext["enqueued"]`` plus the item's limit (``wait``, or its
/// ``@hint max_wait_ms`` when smaller).
fn sow_due(py: Python<'_>, wait: f64, item: &Obj) -> PyResult<f64> {
    let ext = item.bind(py).getattr(intern!(py, "ext"))?;
    let enq = as_f64(&ext.call_method1(intern!(py, "get"), ("enqueued", 0.0))?)?;
    let hint = ext.call_method1(intern!(py, "get"), ("hint", PyDict::new(py)))?;
    let h = hint.call_method1(intern!(py, "get"), ("max_wait_ms",))?;
    let limit = if h.is_none() {
        wait
    } else {
        let ms: f64 = py
            .get_type::<pyo3::types::PyFloat>()
            .call1((h,))?
            .extract()?;
        wait.min(ms / 1000.0)
    };
    Ok(enq + limit)
}

/// ``item.ext.get(name)`` as a truth value.
pub fn flag(py: Python<'_>, item: &Obj, name: &str) -> PyResult<bool> {
    truthy_get(&item.bind(py).getattr(intern!(py, "ext"))?, name)
}

pub fn truthy_get(ext: &Bound<'_, PyAny>, name: &str) -> PyResult<bool> {
    ext.call_method1(intern!(ext.py(), "get"), (name,))?
        .is_truthy()
}
