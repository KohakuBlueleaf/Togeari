//! Bindings of the runtime: `togeari._rs.Core` (the native dispatcher; the Python class
//! `togeari.core.runtime.native.NativeDispatcher` subclasses it with the thread loop and the
//! parts that stay Python), the inbox's Python methods and the work-item `Owner`.

use std::sync::atomic::{AtomicU8, Ordering};

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use super::core::Core;
use super::inbox::Inbox;
use super::state::{effective, InfRef, JobRef, Key, Obj, SlotState};

/// The core, owned by its dispatcher instance. Only the dispatcher thread runs it; another
/// thread reads it with the GIL held and outside any GIL-free region (``outside_region``).
struct Shared(Core);

unsafe impl Send for Shared {}
unsafe impl Sync for Shared {}

/// A base class: the Python subclass's own ``__dict__`` (which the collector sees) holds its
/// attributes.
#[pyclass(name = "Core", module = "togeari._rs", subclass, frozen)]
pub struct PyCore {
    core: Shared,
}

impl PyCore {
    fn c(&self) -> &Core {
        &self.core.0
    }
}

fn job_of(core: &Core, job: &Bound<'_, PyAny>) -> PyResult<Option<JobRef>> {
    let id: u64 = match job.extract::<u64>() {
        Ok(i) => i,
        Err(_) => job.getattr(intern!(job.py(), "id"))?.extract()?,
    };
    Ok(core.job(id))
}

// ------------------------------------------------------------ work-item owners
const FATES: [Option<&str>; 3] = [None, Some("squashed"), Some("retired")];

/// What a work item's ``owner`` names: the statement's inflight id, its job (``job``, a view;
/// ``job_id``), instruction, order key, entry and lane; ``fate`` is set when the inflight is
/// squashed or retired.
#[pyclass(module = "togeari._rs", frozen)]
pub struct Owner {
    #[pyo3(get)]
    pub inf: u64,
    #[pyo3(get)]
    pub job_id: u64,
    #[pyo3(get)]
    pub instr: Obj,
    #[pyo3(get)]
    pub key: Obj,
    #[pyo3(get)]
    pub entry: Obj,
    #[pyo3(get)]
    pub lane: Obj,
    fate: AtomicU8,
    // The dispatcher (weakly) and the view class of its jobs
    d: Obj,
    view: Obj,
}

impl Owner {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        inf: u64,
        job_id: u64,
        instr: Obj,
        key: Obj,
        entry: Obj,
        lane: Obj,
        d: Obj,
        view: Obj,
    ) -> Owner {
        Owner { inf, job_id, instr, key, entry, lane, fate: AtomicU8::new(0), d, view }
    }

    pub fn set_fate(&self, fate: &str) {
        let i = FATES.iter().position(|f| *f == Some(fate)).unwrap_or(0) as u8;
        self.fate.store(i, Ordering::Release);
    }

    pub fn fate(&self) -> Option<&'static str> {
        FATES[self.fate.load(Ordering::Acquire) as usize]
    }
}

#[pymethods]
impl Owner {
    #[getter(fate)]
    fn py_fate(&self) -> Option<&'static str> {
        self.fate()
    }

    /// The owning job as a live view (empty once it ended).
    #[getter]
    fn job<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let d = self.d.bind(py).call0()?;
        self.view.bind(py).call1((d, self.job_id))
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for o in [
            &self.instr,
            &self.key,
            &self.entry,
            &self.lane,
            &self.d,
            &self.view,
        ] {
            visit.call(o)?;
        }
        Ok(())
    }
}

// ------------------------------------------------------------ the inbox
#[pymethods]
impl Inbox {
    #[new]
    fn py_new() -> Self {
        Inbox::new()
    }

    #[pyo3(name = "put")]
    fn py_put(&self, item: Obj) {
        self.put(item);
    }

    #[pyo3(signature = (block = true, timeout = None))]
    fn get(&self, py: Python<'_>, block: bool, timeout: Option<f64>) -> PyResult<Obj> {
        if timeout.is_some_and(|t| t < 0.0) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "'timeout' must be a non-negative number",
            ));
        }
        let got = if block {
            self.wait(py, timeout)?
        } else {
            self.q.lock().expect("the inbox lock").pop_front()
        };
        got.ok_or_else(|| match py.import("queue").and_then(|m| m.getattr("Empty")) {
            Ok(cls) => PyErr::from_type(cls.cast_into().expect("a type"), ()),
            Err(e) => e,
        })
    }

    fn get_nowait(&self, py: Python<'_>) -> PyResult<Obj> {
        self.get(py, false, None)
    }

    fn empty(&self) -> bool {
        self.is_empty()
    }

    fn qsize(&self) -> usize {
        self.q.lock().expect("the inbox lock").len()
    }

    /// Every queued event at once, oldest first.
    fn drain<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.take_all())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(q) = self.q.try_lock() {
            for ev in q.iter() {
                visit.call(ev)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        if let Ok(mut q) = self.q.try_lock() {
            q.clear();
        }
    }
}

// ------------------------------------------------------------ the core
#[pymethods]
impl PyCore {
    #[new]
    #[pyo3(signature = (machine, seed = 0))]
    fn new(machine: &Bound<'_, PyAny>, seed: i64) -> PyResult<Self> {
        Ok(PyCore { core: Shared(Core::new(machine, seed)?) })
    }

    /// Bind the dispatcher instance (weakly) and its inbox; called once from ``__init__``.
    fn _bind(slf: &Bound<'_, Self>, inbox: &Bound<'_, Inbox>) -> PyResult<()> {
        let py = slf.py();
        let c = slf.get().c();
        let r = py.import("weakref")?.getattr("ref")?.call1((slf,))?;
        *c.shell.borrow_mut() = Some(r.unbind());
        *c.inbox.borrow_mut() = Some(inbox.clone().unbind());
        Ok(())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let c = self.c();
        if c.gate.active() {
            return c.h.visit(&visit);
        }
        c.traverse(visit)
    }

    fn __clear__(&self) {
        if !self.c().gate.active() {
            self.c().teardown();
        }
    }

    // ------------------------------------------------------------ the loop
    fn cycle(&self, py: Python<'_>) -> PyResult<()> {
        self.c().outside_region(py);
        self.c().cycle(py)
    }

    fn after_cycle(&self, py: Python<'_>) -> PyResult<()> {
        self.c().outside_region(py);
        self.c().after_cycle(py)
    }

    fn next_timeout(&self, py: Python<'_>) -> PyResult<Option<f64>> {
        self.c().outside_region(py);
        self.c().next_timeout(py)
    }

    fn begin_stop(&self, py: Python<'_>, drain: bool) -> PyResult<()> {
        self.c().outside_region(py);
        self.c().begin_stop(py, drain)
    }

    fn event_error(&self, py: Python<'_>, kind: &str, exc: &Bound<'_, PyAny>) {
        self.c().outside_region(py);
        let e = PyErr::from_value(exc.clone());
        self.c().event_error(py, kind, &e);
    }

    // ------------------------------------------------------------ jobs and lanes
    #[pyo3(signature = (item, handle = None))]
    fn start_job(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        handle: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        self.c().outside_region(py);
        self.c()
            .start_job(py, item, handle.filter(|h| !h.is_none()))
    }

    #[pyo3(signature = (item, result, unit = None))]
    fn deliver(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        result: &Bound<'_, PyAny>,
        unit: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        self.c().outside_region(py);
        self.c()
            .deliver(py, item, result, unit.filter(|u| !u.is_none()))
    }

    fn before_submit<'py>(
        &self,
        py: Python<'py>,
        lane: &Bound<'py, PyAny>,
        unit: &Bound<'py, PyAny>,
        items: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.c().outside_region(py);
        self.c().before_submit(py, lane, unit, items)
    }

    fn cancel_job(&self, py: Python<'_>, job: &Bound<'_, PyAny>, message: &str) -> PyResult<()> {
        self.c().outside_region(py);
        match job_of(self.c(), job)? {
            Some(j) => self.c().cancel_job(py, &j, message),
            None => Ok(()),
        }
    }

    fn pause_job(&self, py: Python<'_>, job: &Bound<'_, PyAny>) -> PyResult<bool> {
        self.c().outside_region(py);
        Ok(match job_of(self.c(), job)? {
            Some(j) => {
                j.borrow_mut().paused = true;
                true
            }
            None => false,
        })
    }

    fn resume_job(&self, py: Python<'_>, job: &Bound<'_, PyAny>) -> PyResult<bool> {
        let c = self.c();
        c.outside_region(py);
        Ok(match job_of(c, job)? {
            Some(j) => {
                let held = {
                    let mut b = j.borrow_mut();
                    b.paused = false;
                    std::mem::take(&mut b.held)
                };
                c.ready.borrow_mut().extend(held);
                c.mark_dirty(j.borrow().id);
                true
            }
            None => false,
        })
    }

    fn release_freed(&self, py: Python<'_>) {
        self.c().outside_region(py);
        self.c().release_freed();
    }

    fn admit(&self, py: Python<'_>) -> PyResult<()> {
        self.c().outside_region(py);
        self.c().admit(py)
    }

    fn job_count(&self, py: Python<'_>) -> usize {
        self.c().outside_region(py);
        self.c().jobs.borrow().len()
    }

    fn window_total(&self, py: Python<'_>) -> usize {
        self.c().outside_region(py);
        self.c().window_total()
    }

    /// Every resident job's record of death: ``(key, entry, outputs, handle)``; the pool empties.
    fn drain_dead<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let c = self.c();
        c.outside_region(py);
        let jobs: Vec<JobRef> = c.jobs.borrow_mut().drain(..).map(|(_, j)| j).collect();
        let mut rows = Vec::with_capacity(jobs.len());
        for j in &jobs {
            let mut b = j.borrow_mut();
            b.ended = true;
            let outs: Vec<Obj> = b
                .entry
                .outputs
                .iter()
                .flatten()
                .map(|(o, _)| o.clone_ref(py).into_any())
                .collect();
            rows.push((
                b.key.clone_ref(py),
                b.entry.py_name.clone_ref(py).into_any(),
                outs,
                b.handle.as_ref().map(|h| h.clone_ref(py)),
            ));
        }
        for j in &jobs {
            Core::let_go(j);
        }
        let out = PyList::empty(py);
        for (key, entry, outs, handle) in rows {
            let handle = handle.unwrap_or_else(|| py.None());
            out.append((key, entry, PyList::new(py, outs)?, handle))?;
        }
        Ok(out)
    }

    /// Region work and GIL offers so far: ``regions`` (``regions_released`` +
    /// ``regions_held``), ``offers`` (released regions + ``pure_offers``), ``free_steps``,
    /// ``gil_steps``, ``free_orders``, ``gil_orders``, ``free_retires``, ``gil_retires``.
    fn native_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.c().outside_region(py);
        let r = self.c().regions.get();
        let d = PyDict::new(py);
        d.set_item("regions", r.regions)?;
        d.set_item("regions_released", r.regions_released)?;
        d.set_item("regions_held", r.regions_held)?;
        d.set_item("offers", r.offers)?;
        d.set_item("pure_offers", r.pure_offers)?;
        d.set_item("free_steps", r.free_steps)?;
        d.set_item("gil_steps", r.gil_steps)?;
        d.set_item("free_orders", r.free_orders)?;
        d.set_item("gil_orders", r.gil_orders)?;
        d.set_item("free_retires", r.free_retires)?;
        d.set_item("gil_retires", r.gil_retires)?;
        Ok(d)
    }

    fn inline_totals<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.c().outside_region(py);
        let totals: Vec<(String, f64)> = self
            .c()
            .inline
            .borrow()
            .iter()
            .map(|(k, st)| (k.clone(), st.total))
            .collect();
        let d = PyDict::new(py);
        for (k, v) in totals {
            d.set_item(k, v)?;
        }
        Ok(d)
    }

    // ------------------------------------------------------------ views
    #[getter]
    fn jobs<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyDict>> {
        let py = slf.py();
        let c = slf.get().c();
        c.outside_region(py);
        let d = PyDict::new(py);
        let ids: Vec<u64> = c.jobs.borrow().keys().copied().collect();
        for id in ids {
            d.set_item(id, c.h.job_view.bind(py).call1((slf, id))?)?;
        }
        Ok(d)
    }

    #[getter]
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.c().outside_region(py);
        self.c().stats_dict(py)
    }

    #[getter]
    fn pool_bytes(&self, py: Python<'_>) -> i64 {
        self.c().outside_region(py);
        self.c().pool_bytes.get()
    }

    #[getter]
    fn ready(&self, py: Python<'_>) -> Vec<u64> {
        self.c().outside_region(py);
        self.c()
            .ready
            .borrow()
            .iter()
            .map(|i| i.borrow().id)
            .collect()
    }

    #[getter]
    fn dirty(&self, py: Python<'_>) -> Vec<u64> {
        self.c().outside_region(py);
        self.c().dirty.borrow().iter().copied().collect()
    }

    #[getter]
    fn clock(&self, py: Python<'_>) -> Obj {
        self.c().outside_region(py);
        self.c().clock.borrow().clone_ref(py)
    }

    #[setter]
    fn set_clock(&self, py: Python<'_>, clock: Obj) {
        self.c().outside_region(py);
        *self.c().clock.borrow_mut() = clock;
    }

    /// A resident job as ``JobView`` reads it, or None: ``(key, entry, inputs, paused, issued,
    /// unfinished, runnable, stats, held)``.
    fn _job_info<'py>(&self, py: Python<'py>, id: u64) -> PyResult<Option<Bound<'py, PyTuple>>> {
        self.c().outside_region(py);
        let Some(j) = self.c().job(id) else {
            return Ok(None);
        };

        // Copied out first: building Python objects may run the collector
        let (key, entry, inputs, paused, issued, unfinished, runnable, held) = {
            let b = j.borrow();
            (
                b.key.clone_ref(py),
                b.entry.py.clone_ref(py),
                b.inputs.clone_ref(py),
                b.paused,
                b.issued,
                b.unfinished,
                b.runnable.len(),
                b.held.len(),
            )
        };
        let stats = j.borrow().stats.snapshot(py);
        let t = PyTuple::new(
            py,
            [
                key.into_bound(py),
                entry.into_bound(py),
                inputs.into_bound(py).into_any(),
                paused.into_pyobject(py)?.to_owned().into_any(),
                issued.into_pyobject(py)?.into_any(),
                unfinished.into_pyobject(py)?.into_any(),
                runnable.into_pyobject(py)?.into_any(),
                stats.to_dict(py)?.into_any(),
                held.into_pyobject(py)?.into_any(),
            ],
        )?;
        Ok(Some(t))
    }

    /// A resident job's window as ``(instr, key, state, item)`` rows.
    fn _job_window<'py>(&self, py: Python<'py>, id: u64) -> PyResult<Bound<'py, PyList>> {
        self.c().outside_region(py);
        let out = PyList::empty(py);
        let Some(j) = self.c().job(id) else {
            return Ok(out);
        };
        let rows: Vec<InfRef> = j.borrow().window.live().to_vec();
        for x in rows {
            let (instr, key, state, item) = {
                let b = x.borrow();
                (
                    b.ins.py.clone_ref(py),
                    b.key.clone(),
                    b.state.name(),
                    b.item.as_ref().map(|i| i.clone_ref(py)),
                )
            };
            let item = item.unwrap_or_else(|| py.None());
            out.append((instr, key.to_py(py), state, item))?;
        }
        Ok(out)
    }

    /// A resident job's strands as ``(id, state, pc, next key, spawner)`` rows.
    fn _job_strands<'py>(&self, py: Python<'py>, id: u64) -> PyResult<Bound<'py, PyList>> {
        self.c().outside_region(py);
        let out = PyList::empty(py);
        let Some(j) = self.c().job(id) else {
            return Ok(out);
        };
        let rows: Vec<_> = j.borrow().strands.values().cloned().collect();
        for s in rows {
            let (id, state, pc, key, spawner) = {
                let b = s.borrow();
                (b.id, b.state.name(), b.pc, b.next_key(), b.spawn.is_some())
            };
            out.append((id, state, pc, key.to_py(py), spawner))?;
        }
        Ok(out)
    }

    /// Slots bound to a name in any live strand, each counted once: ``(ready, pending)``.
    fn _slot_counts(&self, py: Python<'_>, id: u64) -> (usize, usize) {
        self.c().outside_region(py);
        let Some(j) = self.c().job(id) else {
            return (0, 0);
        };
        let last = Key::last();
        let mut seen = std::collections::HashMap::new();
        for s in j.borrow().strands.values() {
            if let Some(n) = &s.borrow().names {
                for slot in n.to_dict().values() {
                    let sid = slot.borrow().id;
                    seen.entry(sid).or_insert_with(|| {
                        effective(slot, &last).borrow().state == SlotState::Ready
                    });
                }
            }
        }
        let ready = seen.values().filter(|r| **r).count();
        (ready, seen.len() - ready)
    }
}

/// Live Rust objects of every core in this process: jobs, strands, inflights, slots.
#[pyfunction]
fn live_objects<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    for (k, v) in super::state::live() {
        d.set_item(k, v)?;
    }
    Ok(d)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyCore>()?;
    m.add_class::<Inbox>()?;
    m.add_class::<Owner>()?;
    m.add_function(wrap_pyfunction!(live_objects, m)?)?;
    Ok(())
}
