//! The Python side the core calls into: the machine's parts, the helper functions values go
//! through, and the classes of what it builds (work items, results, records, views).

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};
use pyo3::{PyTraverseError, PyVisit};

use super::builtins::Builtins;
use super::state::{Obj, Val};
use crate::contracts::digest::Digests;

/// The knobs the core reads, by their interned names.
#[derive(Clone, Copy)]
pub enum K {
    Window,
    RetireBuffer,
    StepBudget,
    RunAhead,
    Mutates,
    FingerprintInputs,
    AssertAlias,
    SnapshotEvery,
    Record,
    RecordLimit,
    EventSample,
    SquashReuse,
    SquashReuseBytes,
    InlineWarnUs,
    GcThreshold,
    GilOfferUs,
    Issue,
    Admission,
}

const KNOBS: [&str; 18] = [
    "window",
    "retire_buffer",
    "step_budget",
    "run_ahead",
    "mutates",
    "fingerprint_inputs",
    "assert_alias",
    "snapshot_every",
    "record",
    "record_limit",
    "event_sample",
    "squash_reuse",
    "squash_reuse_bytes",
    "inline_warn_us",
    "gc_threshold",
    "gil_offer_us",
    "issue",
    "admission",
];

pub struct Host {
    pub machine: Obj,
    pub code: Obj,
    pub mem: Obj,
    pub knobs: Py<PyDict>,
    knob_keys: Vec<Py<PyString>>,
    pub plugins: Obj,
    pub by_hook: Py<PyDict>,
    pub bus: Obj,
    pub bus_subs: Py<PyDict>,
    pub bus_listeners: Py<PyList>,
    pub bus_by_kind: Py<PyDict>,
    pub store: Option<Obj>,
    pub lanes: Obj,
    pub protocol: Obj,
    pub record_rng: Obj,
    pub event_rng: Obj,

    // Value helpers
    pub dg: Digests,
    pub copy_value: Obj,
    pub kept_copy: Obj,
    pub value_bytes: Obj,
    pub deepcopy: Obj,
    pub undef: Obj,
    pub undef_val: Val,
    pub miss: Obj,

    // Classes
    pub runtime_fault: Obj,
    pub work_item: Obj,
    pub ok: Obj,
    pub item_error: Obj,
    pub unit_error: Obj,
    pub cancelled: Obj,
    pub blocked: Obj,
    pub plugin_block_error: Obj,
    pub job_record: Obj,
    pub source_item: Obj,
    pub ready_view: Obj,
    pub pool_view: Obj,
    pub signals: Obj,
    pub ready_inf: Obj,
    pub job_view: Obj,
    pub hint_order: Obj,

    // Snapshots, logs
    pub snapshot_key: Obj,
    pub legacy_snapshot_key: Obj,
    pub snapshot_candidate: Obj,
    pub snapshot_encode: Obj,
    pub snapshot_prepare: Obj,
    pub observe_execution: Obj,
    pub execution_context: Obj,
    pub snapshot_rejection: Obj,
    pub result_execution: Obj,
    pub warn_unverified: Obj,
    pub pickle_dumps: Obj,
    pub pickle_loads: Obj,
    pub log: Obj,
    pub snap_log: Obj,

    pub builtins: Builtins,
}

fn attr(py: Python<'_>, module: &str, name: &str) -> PyResult<Obj> {
    Ok(py.import(module)?.getattr(name)?.unbind())
}

impl Host {
    pub fn new(machine: &Bound<'_, PyAny>, seed: i64) -> PyResult<Host> {
        let py = machine.py();
        let m = |name: &str| machine.getattr(name);
        let code = m("code")?;
        let plugins = m("plugins")?;
        let bus = m("bus")?;
        let store = m("store")?;
        let random = py.import("random")?.getattr("Random")?;
        let logging = py.import("logging")?;
        let rt = "togeari.core.runtime";
        let res = "togeari.core.contracts.results";
        Ok(Host {
            mem: code.getattr("mem")?.unbind(),
            code: code.unbind(),
            knobs: m("knobs")?.cast_into::<PyDict>()?.unbind(),
            knob_keys: KNOBS
                .iter()
                .map(|k| PyString::intern(py, k).unbind())
                .collect(),
            by_hook: plugins.getattr("by_hook")?.cast_into::<PyDict>()?.unbind(),
            plugins: plugins.unbind(),
            bus_subs: bus.getattr("subs")?.cast_into::<PyDict>()?.unbind(),
            bus_listeners: bus.getattr("listeners")?.cast_into::<PyList>()?.unbind(),
            bus_by_kind: bus.getattr("by_kind")?.cast_into::<PyDict>()?.unbind(),
            bus: bus.unbind(),
            store: (!store.is_none()).then(|| store.unbind()),
            lanes: m("lanes")?.unbind(),
            protocol: m("defaults")?.getattr("protocol")?.unbind(),
            record_rng: random.call1((seed,))?.unbind(),
            event_rng: random.call1((seed + 1,))?.unbind(),
            dg: Digests::new(py)?,
            copy_value: attr(py, "togeari.core.contracts.io", "copy_value")?,
            kept_copy: attr(py, &format!("{rt}.values"), "kept_copy")?,
            value_bytes: attr(py, &format!("{rt}.values"), "value_bytes")?,
            deepcopy: attr(py, "copy", "deepcopy")?,
            undef: attr(py, &format!("{rt}.kir"), "UNDEF")?,
            undef_val: std::rc::Rc::new(attr(py, &format!("{rt}.kir"), "UNDEF")?),
            miss: attr(py, "togeari.core.contracts.io", "MISS")?,
            runtime_fault: attr(py, &format!("{rt}.kir"), "RuntimeFault")?,
            work_item: attr(py, "togeari.core.contracts.workitem", "WorkItem")?,
            ok: attr(py, res, "Ok")?,
            item_error: attr(py, res, "ItemError")?,
            unit_error: attr(py, res, "UnitError")?,
            cancelled: attr(py, res, "Cancelled")?,
            blocked: attr(py, res, "Blocked")?,
            plugin_block_error: attr(py, "togeari.core.plugin.base", "PluginBlockError")?,
            job_record: attr(py, "togeari.core.contracts.io", "JobRecord")?,
            source_item: attr(py, "togeari.core.contracts.io", "SourceItem")?,
            ready_view: attr(py, "togeari.core.contracts.views", "ReadyView")?,
            pool_view: attr(py, "togeari.core.contracts.views", "PoolView")?,
            signals: attr(py, &format!("{rt}.pool.signals"), "Signals")?,
            ready_inf: attr(py, &format!("{rt}.native"), "ReadyInf")?,
            job_view: attr(py, &format!("{rt}.native"), "JobView")?,
            hint_order: attr(py, "togeari.core.policies.issue", "HintPriority")?,
            snapshot_key: attr(py, &format!("{rt}.retire.snapshot"), "snapshot_key")?,
            legacy_snapshot_key: attr(py, &format!("{rt}.retire.snapshot"), "legacy_snapshot_key")?,
            snapshot_candidate: attr(py, &format!("{rt}.retire.snapshot"), "load_candidate")?,
            snapshot_encode: attr(py, &format!("{rt}.retire.codec"), "encode_snapshot")?,
            snapshot_prepare: attr(py, &format!("{rt}.retire.codec"), "prepare_restore")?,
            observe_execution: attr(py, "togeari.core.program.execution", "observe_execution")?,
            execution_context: attr(py, "togeari.core.program.execution", "execution_context")?,
            snapshot_rejection: attr(py, &format!("{rt}.retire.snapshot"), "snapshot_rejection")?,
            result_execution: attr(py, &format!("{rt}.backend.persistence"), "result_execution")?,
            warn_unverified: attr(py, "togeari.core.program.execution", "warn_unverified")?,
            pickle_dumps: attr(py, "pickle", "dumps")?,
            pickle_loads: attr(py, "pickle", "loads")?,
            log: logging
                .call_method1("getLogger", ("togeari.dispatcher",))?
                .unbind(),
            snap_log: logging
                .call_method1("getLogger", ("togeari.snapshot",))?
                .unbind(),
            builtins: Builtins::new(py)?,
            machine: machine.clone().unbind(),
        })
    }

    pub fn knob<'py>(&self, py: Python<'py>, k: K) -> PyResult<Bound<'py, PyAny>> {
        let key = self.knob_keys[k as usize].bind(py);
        match self.knobs.bind(py).get_item(key)? {
            Some(v) => Ok(v),
            None => Err(pyo3::exceptions::PyKeyError::new_err(key.clone().unbind())),
        }
    }

    /// A number knob (None for None), compared as Python compares an int count with it.
    pub fn knob_f64(&self, py: Python<'_>, k: K) -> PyResult<Option<f64>> {
        crate::lanes::lane::opt_f64(&self.knob(py, k)?)
    }

    /// Whether any enabled plugin overrides ``hook``.
    pub fn has_hook(&self, py: Python<'_>, hook: &str) -> PyResult<bool> {
        match self.by_hook.bind(py).get_item(hook)? {
            Some(v) => v.is_truthy(),
            None => Ok(false),
        }
    }

    pub fn is_undef(&self, py: Python<'_>, v: &Obj) -> bool {
        v.bind(py).is(self.undef.bind(py))
    }

    /// Log at ``level`` on the dispatcher's logger, with the error's traceback when given.
    pub fn log(&self, py: Python<'_>, level: &str, msg: &str, err: Option<&PyErr>) {
        let kw = PyDict::new(py);
        if let Some(e) = err {
            let _ = kw.set_item(intern!(py, "exc_info"), e.value(py));
        }
        let _ = self
            .log
            .bind(py)
            .call_method(level, (PyString::new(py, msg),), Some(&kw));
    }

    /// Every Python object the host holds (``undef_val`` is visited with the slots sharing it).
    pub fn visit(&self, v: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        for o in [
            &self.machine,
            &self.code,
            &self.mem,
            &self.plugins,
            &self.bus,
            &self.lanes,
            &self.protocol,
            &self.record_rng,
            &self.event_rng,
            &self.copy_value,
            &self.kept_copy,
            &self.value_bytes,
            &self.deepcopy,
            &self.undef,
            &self.miss,
            &self.runtime_fault,
            &self.work_item,
            &self.ok,
            &self.item_error,
            &self.unit_error,
            &self.cancelled,
            &self.blocked,
            &self.plugin_block_error,
            &self.job_record,
            &self.source_item,
            &self.ready_view,
            &self.pool_view,
            &self.signals,
            &self.ready_inf,
            &self.job_view,
            &self.hint_order,
            &self.snapshot_key,
            &self.legacy_snapshot_key,
            &self.snapshot_candidate,
            &self.snapshot_encode,
            &self.snapshot_prepare,
            &self.observe_execution,
            &self.execution_context,
            &self.snapshot_rejection,
            &self.result_execution,
            &self.warn_unverified,
            &self.pickle_dumps,
            &self.pickle_loads,
            &self.log,
            &self.snap_log,
        ] {
            v.call(o)?;
        }
        v.call(&self.knobs)?;
        v.call(&self.by_hook)?;
        v.call(&self.bus_subs)?;
        v.call(&self.bus_listeners)?;
        v.call(&self.bus_by_kind)?;
        for k in &self.knob_keys {
            v.call(k)?;
        }
        if let Some(s) = &self.store {
            v.call(s)?;
        }
        self.dg.visit(v)?;
        self.builtins.visit(v)
    }
}

/// `except Exception` (not `BaseException`).
pub fn is_exception(py: Python<'_>, e: &PyErr) -> bool {
    e.is_instance_of::<pyo3::exceptions::PyException>(py)
}
