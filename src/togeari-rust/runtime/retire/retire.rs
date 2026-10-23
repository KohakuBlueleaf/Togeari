//! Retire in order-key order; finish a job; snapshot at retire boundaries (spec/03 §3.9, §3.10).

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyDict, PyList, PySet, PyString, PyTuple};

use crate::program::core::{Instr, Op, StoreMode};
use crate::runtime::core::Core;
use crate::runtime::events::Exc;
use crate::runtime::frontend::fetch::Flow;
use crate::runtime::host::{is_exception, K};
use crate::runtime::state::slot::hold;
use crate::runtime::state::{deref, effective, InfRef, InfState, JobRef, Key, Names, SlotState};

/// A retired `mutates` call: its version becomes the oldest one its values keep.
fn fold_versions(x: &InfRef) {
    let b = x.borrow();
    for v in b.versions.iter().flatten() {
        let base = deref(&v.before);
        let mut s = base.borrow_mut();
        s.muts
            .retain(|(k, ver)| *k >= b.key && ver.borrow().state != SlotState::Squashed);
    }
}

fn mutable(v: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(v.is_instance_of::<PyList>()
        || v.is_instance_of::<PyDict>()
        || v.is_instance_of::<PySet>()
        || v.is_instance_of::<PyByteArray>()
        || v.hasattr(intern!(v.py(), "__dict__"))?)
}

/// The knobs retire reads, once per cycle.
pub struct RetireCaps {
    pub assert_alias: bool,
    pub snapshot_every: Option<f64>,
    pub store: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Stage {
    Walk,
    Snapshot,
    Wake,
    Finish,
    Done,
}

/// One job's retire, resumable after a step that needed the GIL.
pub struct RetirePass {
    bound: Option<Key>,
    retired: i64,
    stage: Stage,
}

impl RetirePass {
    pub fn new(job: &JobRef) -> RetirePass {
        RetirePass { bound: job.borrow_mut().oldest_live_key(), retired: 0, stage: Stage::Walk }
    }
}

impl Core {
    /// Keep provenance beside the unchanged five-field replay records.
    pub fn record_origin(
        &self,
        py: Python<'_>,
        job: &JobRef,
        ins: &Instr,
        index: usize,
    ) -> PyResult<()> {
        let origins = job
            .borrow_mut()
            .stats
            .extra
            .entry("record_origins".into())
            .or_insert_with(|| PyList::empty(py).into_any().unbind())
            .clone_ref(py);
        ins.py
            .bind(py)
            .call_method1(intern!(py, "record_origin"), (origins, index))?;
        Ok(())
    }

    /// Aligned sidecar for logical invocations; replay's five-field tuples remain unchanged.
    pub(crate) fn record_invocation(
        &self,
        py: Python<'_>,
        job: &JobRef,
        inf: &InfRef,
        index: usize,
    ) -> PyResult<()> {
        let (invocation, attempts, resolution) = {
            let inf = inf.borrow();
            (
                inf.invocation_id.clone().ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err(
                        "recorded call has no invocation identity",
                    )
                })?,
                inf.attempt_ids.clone(),
                inf.resolution,
            )
        };
        let sidecar = job
            .borrow_mut()
            .stats
            .extra
            .entry("record_invocations".into())
            .or_insert_with(|| PyList::empty(py).into_any().unbind())
            .clone_ref(py);
        self.h.record_invocation.bind(py).call1((
            sidecar.bind(py),
            index,
            invocation,
            attempts,
            resolution,
        ))?;
        Ok(())
    }

    /// Whether retiring ``x`` needs Python: a record, the alias check, a positional store.
    fn retire_needs_gil(&self, job: &JobRef, x: &InfRef, caps: &RetireCaps) -> bool {
        if caps.assert_alias || job.borrow().record.is_some() {
            return true;
        }
        let b = x.borrow();
        if b.collector.is_some() {
            return true;
        }
        let positional = b.ins.binding.as_ref().map(|bd| bd.store) == Some(StoreMode::Positional);
        b.store_key.is_some() && !b.hit && positional
    }

    /// Retire the job's done statements in order-key order, then snapshot, wake blocked work and
    /// finish; without the GIL (``py`` None) it stops, resumably, before a step that needs it.
    pub fn retire(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        pass: &mut RetirePass,
        caps: &RetireCaps,
    ) -> PyResult<Flow> {
        let need = Flow::NeedGil(0);
        if pass.stage == Stage::Walk {
            loop {
                let x = {
                    let j = job.borrow();
                    if j.window.is_empty() || j.ended {
                        break;
                    }
                    j.window.first().expect("a window head")
                };
                let (state, key, faulted) = {
                    let b = x.borrow();
                    (b.state, b.key.clone(), b.fault.is_some())
                };
                if state != InfState::Done || pass.bound.as_ref().is_some_and(|b| key >= *b) {
                    break;
                }
                if faulted || x.borrow().ins.semantic_emit || self.retire_needs_gil(job, &x, caps) {
                    let Some(py) = py else {
                        self.count_region(|r| r.gil_retires += 1);
                        return Ok(need);
                    };
                    if faulted {
                        self.take_fault(py, job, &x)?;
                        pass.bound = job.borrow_mut().oldest_live_key();
                        continue;
                    }
                } else if py.is_none() {
                    self.count_region(|r| r.free_retires += 1);
                }
                if x.borrow().collector.is_some() && x.borrow().collect_index.is_some() {
                    let py = py.expect("collector retire holds GIL");
                    let slot = {
                        let b = x.borrow();
                        effective(&b.inputs[0], &b.key)
                    };
                    let undef = slot
                        .borrow()
                        .value
                        .as_ref()
                        .is_some_and(|v| self.h.is_undef(py, v));
                    if undef {
                        self.fault(
                            py,
                            &x,
                            Exc::Runtime("collect: yielded value is not defined".into()),
                            false,
                        )?;
                        self.take_fault(py, job, &x)?;
                        pass.bound = job.borrow_mut().oldest_live_key();
                        continue;
                    }
                }
                let semantic_emit = x.borrow().ins.semantic_emit;
                if semantic_emit
                    && !self.event_before_retire(py.expect("emit retire holds GIL"), job, &x)?
                {
                    break;
                }
                job.borrow_mut().window.pop_first();
                x.borrow_mut().state = InfState::Retired;
                job.borrow_mut().checkpoint_watermark = Some(x.borrow().key.clone());
                pass.retired += 1;
                self.retire_one(py, job, &x, caps)?;
                if semantic_emit {
                    self.event_retired(py.expect("emit retire holds GIL"), job, &x)?;
                }
                if x.borrow().versions.is_some() {
                    fold_versions(&x);
                }

                // The retired statement lets go of its values, work item, view and slots
                self.disown(&x, "retired");
                x.borrow_mut().release();
                let cancel = job.borrow_mut().event_cancel.take();
                if let Some(reason) = cancel {
                    self.cancel_job(
                        py.expect("deferred event cancellation holds GIL"),
                        job,
                        &reason,
                    )?;
                    break;
                }
            }
            {
                let mut j = job.borrow_mut();
                j.oldest_open_key();
                j.stats.retired += pass.retired;
            }
            if job.borrow().ended {
                pass.stage = Stage::Done;
                return Ok(Flow::Done(0));
            }
            pass.stage = Stage::Snapshot;
        }

        // Snapshot at a retire boundary every `snapshot_every` retired statements
        if pass.stage == Stage::Snapshot {
            let due = caps.snapshot_every.is_some_and(|every| {
                let j = job.borrow();
                every != 0.0 && (j.stats.retired - j.snap_at) as f64 >= every
            });
            if due && caps.store && !job.borrow().snap_off {
                let Some(py) = py else { return Ok(need) };
                self.take_snapshot(py, job)?;
            }
            pass.stage = Stage::Wake;
        }

        // Statements blocked on older work may go now; strands at a cap may fetch again
        if pass.stage == Stage::Wake {
            let jid = job.borrow().id;
            let pending_emit = job.borrow().window.live().iter().any(|inf| {
                let inf = inf.borrow();
                inf.ins.semantic_emit && inf.state == InfState::Done && inf.fault.is_none()
            });
            let blocked = if pending_emit {
                None
            } else {
                self.blocked.borrow_mut().remove(&jid)
            };
            for inf in blocked.into_iter().flatten() {
                if inf.borrow().state == InfState::Ready {
                    self.ready.borrow_mut().push(inf);
                }
            }
            if pass.retired != 0 && !job.borrow().runnable.is_empty() {
                self.mark_dirty(jid);
            }
            pass.stage = Stage::Finish;
        }
        if pass.stage == Stage::Finish {
            if job.borrow().cut.is_some() {
                let Some(py) = py else { return Ok(need) };
                self.checkpoint_progress(py)?;
            }
            let done = {
                let j = job.borrow();
                j.window.is_empty() && j.strands.is_empty()
            };
            if done {
                let Some(py) = py else { return Ok(need) };
                self.finish(py, job)?;
            }
            pass.stage = Stage::Done;
        }
        Ok(Flow::Done(0))
    }

    fn collect_stat(
        &self,
        py: Python<'_>,
        job: &JobRef,
        name: &str,
        peak: Option<usize>,
    ) -> PyResult<()> {
        let mut j = job.borrow_mut();
        let previous = j
            .stats
            .extra
            .get(name)
            .map(|v| v.extract::<usize>(py))
            .transpose()?
            .unwrap_or(0);
        let value = peak.map_or(previous + 1, |v| previous.max(v));
        j.stats
            .extra
            .insert(name.into(), value.into_pyobject(py)?.into_any().unbind());
        Ok(())
    }

    fn hold_collect_slot(&self, job: &JobRef, slot: &crate::runtime::state::SlotRef, size: i64) {
        let old = slot.borrow().held.as_ref().map(|h| h.size);
        let added = if let Some(old) = old {
            let extra = (size - old).max(0);
            slot.borrow_mut().held.as_mut().expect("held").size += extra;
            extra
        } else {
            hold(slot, job.borrow().id, size, &self.freed)
        };
        let mut j = job.borrow_mut();
        j.bytes += added;
        j.stats.bytes_produced += added;
        j.stats.bytes_peak = j.stats.bytes_peak.max(j.bytes);
        self.pool_bytes.set(self.pool_bytes.get() + added);
    }

    fn retire_one(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        x: &InfRef,
        caps: &RetireCaps,
    ) -> PyResult<()> {
        let Some(py) = py else {
            return self.note_stmt(None, "stmt.retire", x);
        };
        let ins = x.borrow().ins.clone();
        let collector = x.borrow().collector.clone();
        if let Some(collector) = collector {
            let index = x.borrow().collect_index;
            let mut c = collector.borrow_mut();
            if !c.active {
                return Ok(());
            }
            if let Some(index) = index {
                if index != c.values.len() {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        "collect: out-of-order retirement",
                    ));
                }
                let slot = {
                    let b = x.borrow();
                    effective(&b.inputs[0], &b.key)
                };
                let value = slot.borrow().value.clone().expect("ready collection value");
                self.hold_collect_slot(job, &slot, self.value_bytes(py, value.bind(py))?);
                c.values.push(value);
                c.slots.push(slot);
                self.hold_collect_slot(job, &c.output, 64 + 16 * c.values.len() as i64);
                self.collect_stat(py, job, "collect_captures", None)?;
                self.collect_stat(py, job, "collect_peak_items", Some(c.values.len()))?;
            } else {
                if c.values.len() != c.count {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        "collect: incomplete retirement",
                    ));
                }
                let value = PyList::new(py, c.values.iter().map(|v| v.bind(py)))?.into_any();
                let size = self.value_bytes(py, &value)?;
                self.fill(py, &c.output, value.unbind())?;
                self.hold_collect_slot(job, &c.output, size);
                self.collect_stat(py, job, "collect_finalizations", None)?;
                job.borrow_mut()
                    .collectors
                    .retain(|other| !std::rc::Rc::ptr_eq(other, &collector));
                c.values.clear();
                c.slots.clear();
                c.snapshot = None;
                c.active = false;
            }
            return Ok(());
        }
        if caps.assert_alias {
            self.alias_check(py, job, x)?;
        }
        let record = job.borrow().record.as_ref().map(|r| r.clone_ref(py));
        if let (Some(rec), Op::Call) = (record, ins.op) {
            let t = {
                let b = x.borrow();
                let none = || py.None().into_bound(py);
                PyTuple::new(
                    py,
                    [
                        ins.node.bind(py).as_any(),
                        &b.dig
                            .as_ref()
                            .map(|d| d.bind(py).clone())
                            .unwrap_or_else(none),
                        PyString::new(py, "ok").as_any(),
                        &b.kept
                            .as_ref()
                            .map(|d| d.bind(py).clone())
                            .unwrap_or_else(none),
                        &b.mutated
                            .as_ref()
                            .map(|d| d.bind(py).clone())
                            .unwrap_or_else(none),
                    ],
                )?
            };
            let rec = rec.bind(py);
            self.record_origin(py, job, &ins, rec.len())?;
            self.record_invocation(py, job, x, rec.len())?;
            rec.append(t)?;
            let limit = self.h.knob(py, K::RecordLimit)?;
            if !limit.is_none() && rec.len() as f64 > crate::lanes::lane::as_f64(&limit)? {
                let msg = PyString::new(py, &format!("more than {} calls", limit.str()?));
                let mut j = job.borrow_mut();
                j.record = None;
                j.stats.extra.shift_remove("record_origins");
                j.stats.extra.shift_remove("record_invocations");
                j.stats
                    .extra
                    .insert("record_skipped".into(), msg.into_any().unbind());
            }
        }

        // A positional result is stored as copied at settle, before any younger in-place change
        let put = {
            let b = x.borrow();
            let positional = ins.binding.as_ref().map(|bd| bd.store) == Some(StoreMode::Positional);
            match &b.store_key {
                Some(k) if !b.hit && positional => {
                    Some((k.clone_ref(py), b.kept.as_ref().map(|v| v.clone_ref(py))))
                }
                _ => None,
            }
        };
        if let Some((k, kept)) = put {
            if self.result_store_allowed(py, job, &ins)? {
                let kept = kept.unwrap_or_else(|| py.None());
                self.store_put(py, &k, kept.bind(py))?;
            }
        }
        self.stmt_event(py, "stmt.retire", x)
    }

    fn take_snapshot(&self, py: Python<'_>, job: &JobRef) -> PyResult<()> {
        let Some(key) = self.snapshot_key(py, job)? else {
            job.borrow_mut().snap_off = true;
            return Ok(());
        };
        let blob = match self.capture(py, job) {
            Ok(b) => b,
            Err(e) if is_exception(py, &e) => {
                let t = e.get_type(py).name()?.to_string();
                let msg = format!("{t}: {}", e.value(py).str()?);
                let text = PyString::new(py, &msg).into_any().unbind();
                let (jid, jkey) = {
                    let mut j = job.borrow_mut();
                    j.snap_off = true;
                    j.stats.extra.insert("snapshot_error".into(), text);
                    (j.id, j.key.clone_ref(py))
                };
                self.stats.borrow_mut().snapshot_errors += 1;
                let text = format!("job {} cannot snapshot: {msg}", jkey.bind(py));
                self.h.log(py, "warning", &text, None);
                if self.wants(py, "snapshot.error", Some(jid))? {
                    let f = PyDict::new(py);
                    f.set_item("job", jid)?;
                    f.set_item("key", jkey)?;
                    f.set_item("error", msg)?;
                    self.emit(py, "snapshot.error", &f)?;
                }
                return Ok(());
            }
            Err(e) => return Err(e),
        };
        if let Some(blob) = blob {
            self.store_put(py, &key, blob.bind(py))?;
            let n = job.borrow().stats.get_int(py, "snapshots");
            let count = (n + 1).into_pyobject(py)?.into_any().unbind();
            let mut j = job.borrow_mut();
            j.snap_at = j.stats.retired;
            j.stats.extra.insert("snapshots".into(), count);
        }
        Ok(())
    }

    /// The job's snapshot key; None without a store or with weak inputs.
    pub fn snapshot_key(&self, py: Python<'_>, job: &JobRef) -> PyResult<Option<Py<PyAny>>> {
        self.snapshot_locator(py, job, 3)
    }

    /// Legacy lookup is diagnosis only; it never authorizes applying old absolute addresses.
    pub(crate) fn legacy_snapshot_key(
        &self,
        py: Python<'_>,
        job: &JobRef,
    ) -> PyResult<Option<Py<PyAny>>> {
        self.snapshot_locator(py, job, 1)
    }

    pub(crate) fn v2_snapshot_key(
        &self,
        py: Python<'_>,
        job: &JobRef,
    ) -> PyResult<Option<Py<PyAny>>> {
        self.snapshot_locator(py, job, 2)
    }

    fn snapshot_locator(
        &self,
        py: Python<'_>,
        job: &JobRef,
        version: u8,
    ) -> PyResult<Option<Py<PyAny>>> {
        let (dig, jkey, addr) = {
            let j = job.borrow();
            (
                j.inputs_dig.as_ref().map(|d| d.clone_ref(py)),
                j.key.clone_ref(py),
                j.entry.addr,
            )
        };
        let Some(dig) = dig else { return Ok(None) };
        if self.h.store.is_none() {
            return Ok(None);
        }
        let region = self.code.borrow().region_of(addr).expect("a region");
        let helper = match version {
            1 => &self.h.legacy_snapshot_key,
            2 => &self.h.v2_snapshot_key,
            _ => &self.h.snapshot_key,
        };
        let k = helper.bind(py).call1((region.hash.bind(py), jkey, dig))?;
        Ok(Some(k.unbind()))
    }

    // ------------------------------------------------------------ debug checks
    /// At retire: every input this statement read and every value it wrote is filled.
    fn alias_check(&self, py: Python<'_>, job: &JobRef, x: &InfRef) -> PyResult<()> {
        let bad = PyList::empty(py);
        {
            let b = x.borrow();
            for ((p, _), s) in b.ins.args.iter().zip(&b.inputs) {
                if effective(s, &b.key).borrow().state != SlotState::Ready {
                    bad.append(p.bind(py))?;
                }
            }
            for (i, s) in b.outs.iter().enumerate() {
                if let Some(s) = s {
                    if s.borrow().state != SlotState::Ready {
                        bad.append(format!("output {i}"))?;
                    }
                }
            }
            for v in b.versions.iter().flatten() {
                if v.version.borrow().state != SlotState::Ready {
                    bad.append(format!("{} after", b.ins.mutates[v.k].bind(py)))?;
                }
            }
        }
        if !bad.is_empty() && job.borrow().failure.is_none() {
            let f = PyDict::new(py);
            f.set_item("kind", "AliasCheck")?;
            f.set_item("node", x.borrow().ins.node.bind(py))?;
            f.set_item("message", format!("at retire, not filled: {}", bad.repr()?))?;
            job.borrow_mut().failure = Some(f.unbind());
        }
        Ok(())
    }

    /// A problem with the names a job ends with, or None.
    fn alias_at_end(
        &self,
        py: Python<'_>,
        job: &JobRef,
        final_names: &Names,
    ) -> PyResult<Option<String>> {
        let last = Key::last();
        let mut squashed = Vec::new();
        let mut mine: Vec<(usize, String)> = Vec::new();
        for (n, slot) in final_names.to_dict() {
            let s = effective(&slot, &last);
            let b = s.borrow();
            let name = self.code.borrow().names.text(n).to_string();
            if b.state != SlotState::Ready {
                squashed.push(name);
                continue;
            }
            if let Some(v) = &b.value {
                if mutable(v.bind(py))? {
                    mine.push((v.as_ptr() as usize, name));
                }
            }
        }
        if !squashed.is_empty() {
            squashed.sort();
            let l = PyList::new(py, squashed)?;
            return Ok(Some(format!("names not filled at the end: {}", l.repr()?)));
        }

        // Mutable values of this job reachable from another resident job
        let others: Vec<JobRef> = self.jobs.borrow().values().cloned().collect();
        for other in others {
            if std::rc::Rc::ptr_eq(&other, job) {
                continue;
            }
            for v in self.values_of(py, &other) {
                let id = v.as_ptr() as usize;
                if let Some((_, name)) = mine.iter().rev().find(|(p, _)| *p == id) {
                    let key = other.borrow().key.bind(py).repr()?.to_string();
                    let name = PyString::new(py, name).repr()?.to_string();
                    return Ok(Some(format!("{name} is shared with job {key}")));
                }
            }
        }
        Ok(None)
    }

    /// Every value a resident job's live names and window hold.
    fn values_of(&self, py: Python<'_>, job: &JobRef) -> Vec<Py<PyAny>> {
        let last = Key::last();
        let mut out = Vec::new();
        let j = job.borrow();
        for st in j.strands.values() {
            if let Some(names) = &st.borrow().names {
                for slot in names.to_dict().values() {
                    let s = effective(slot, &last);
                    let b = s.borrow();
                    if b.state == SlotState::Ready {
                        out.push(
                            b.value
                                .as_ref()
                                .map(|v| v.clone_ref(py))
                                .unwrap_or_else(|| py.None()),
                        );
                    }
                }
            }
        }
        for x in j.window.live() {
            if let Some(values) = &x.borrow().values {
                for v in values.bind(py).values() {
                    out.push(v.unbind());
                }
            }
        }
        out
    }

    // ------------------------------------------------------------ job end
    /// A job with no live strand and an empty window: collect its outputs.
    fn finish(&self, py: Python<'_>, job: &JobRef) -> PyResult<()> {
        let (final_names, entry) = {
            let j = job.borrow();
            (j.final_names.clone(), j.entry.clone())
        };
        let outputs = PyDict::new(py);
        let missing = PyList::empty(py);
        let names: Vec<(Py<PyString>, Option<u32>)> = match (&final_names, &entry.outputs) {
            (None, outs) => outs
                .iter()
                .flatten()
                .map(|(p, n)| (p.clone_ref(py), Some(*n)))
                .collect(),
            (Some(f), None) => {
                let code = self.code.borrow();
                f.keys()
                    .into_iter()
                    .filter(|n| !code.names.text(*n).contains("__"))
                    .map(|n| (code.name_py(py, n).clone().unbind(), Some(n)))
                    .collect()
            }
            (Some(_), Some(outs)) => outs
                .iter()
                .map(|(p, n)| (p.clone_ref(py), Some(*n)))
                .collect(),
        };
        let last = Key::last();
        for (name_py, n) in names {
            let n = n.expect("a name");
            let s = final_names.as_ref().and_then(|f| f.get(n));
            let v = s.map(|s| effective(&s, &last));
            let filled = v.as_ref().and_then(|v| {
                let b = v.borrow();
                if b.state == SlotState::Ready {
                    let val = b
                        .value
                        .as_ref()
                        .map(|x| x.clone_ref(py))
                        .unwrap_or_else(|| py.None());
                    (!self.h.is_undef(py, &val)).then_some(val)
                } else {
                    None
                }
            });
            match filled {
                Some(val) => outputs.set_item(name_py.bind(py), val)?,
                None => missing.append(name_py.bind(py))?,
            }
        }

        // Debug check: every name filled, no mutable value shared with another resident job
        let check = self.h.knob(py, K::AssertAlias)?.is_truthy()?;
        if check && job.borrow().failure.is_none() {
            if let Some(f) = &final_names {
                if let Some(message) = self.alias_at_end(py, job, f)? {
                    let d = PyDict::new(py);
                    d.set_item("kind", "AliasCheck")?;
                    d.set_item("node", py.None())?;
                    d.set_item("message", message)?;
                    job.borrow_mut().failure = Some(d.unbind());
                }
            }
        }
        if !missing.is_empty() && entry.outputs.is_some() {
            let d = PyDict::new(py);
            d.set_item("kind", "KirRuntimeError")?;
            d.set_item("node", entry.py_name.bind(py))?;
            d.set_item(
                "message",
                format!("@def {}: outputs {} not set", entry.name, missing.repr()?),
            )?;
            job.borrow_mut().failure = Some(d.unbind());
        }
        self.end_job(py, job, outputs, missing)
    }
}
