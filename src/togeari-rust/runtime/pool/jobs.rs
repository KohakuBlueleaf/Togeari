//! The job pool: start, end, records (spec/03 §3.10).

use std::collections::HashSet;
use std::rc::Rc;

use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PySet, PyString};

use crate::runtime::core::Core;
use crate::runtime::host::{is_exception, K};
use crate::runtime::state::frame::no_frames;
use crate::runtime::state::key::empty_prefix;
use crate::runtime::state::slot::hold;
use crate::runtime::state::{Entry, InfState, Job, JobRef, NameMap, Param, Slot, Strand};

enum SnapshotAdmission {
    New,
    Resumed,
    Reject(Py<PyDict>),
}

fn snapshot_field<'py>(state: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    state
        .get_item(key)?
        .ok_or_else(|| PyKeyError::new_err(format!("snapshot helper field {key:?} missing")))
}

impl Core {
    /// The shell object (the dispatcher instance this core belongs to).
    pub fn shell<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let r = self.shell.borrow();
        let r = r.as_ref().expect("a bound dispatcher");
        r.bind(py).call0()
    }

    fn where_entry<'py>(
        py: Python<'py>,
        entry: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let w = PyDict::new(py);
        w.set_item("entry", entry)?;
        Ok(w)
    }

    /// The core's description of a code-memory entry, importing its region on first use.
    fn entry_of(&self, py: Python<'_>, e: &Bound<'_, PyAny>) -> PyResult<Rc<Entry>> {
        let name: String = e.getattr(intern!(py, "name"))?.extract()?;
        if let Some((obj, rc)) = self.entries.borrow().get(&name) {
            if obj.bind(py).is(e) {
                return Ok(rc.clone());
            }
        }
        let region = e.getattr(intern!(py, "region"))?;
        crate::program::python::import_region(
            &mut self.code.borrow_mut(),
            self.h.mem.bind(py),
            &region,
        )?;
        let mut params = Vec::new();
        let mut outputs = None;
        {
            let mut code = self.code.borrow_mut();
            for p in e.getattr(intern!(py, "params"))?.try_iter()? {
                let p = p?;
                let pname = p.getattr(intern!(py, "name"))?.cast_into::<PyString>()?;
                params.push(Param {
                    name: code.intern(py, &pname.to_string()),
                    py_name: pname.unbind(),
                    has_default: p.getattr(intern!(py, "has_default"))?.is_truthy()?,
                    default: p.getattr(intern!(py, "default"))?.unbind(),
                });
            }
            let outs = e.getattr(intern!(py, "outputs"))?;
            if !outs.is_none() {
                let mut v = Vec::new();
                for o in outs.try_iter()? {
                    let o = o?.cast_into::<PyString>()?;
                    v.push((o.clone().unbind(), code.intern(py, &o.to_string())));
                }
                outputs = Some(v);
            }
        }
        let entry = Rc::new(Entry {
            py_name: PyString::new(py, &name).unbind(),
            name: name.clone(),
            addr: e.getattr(intern!(py, "addr"))?.extract()?,
            params,
            outputs,
            region_hash: region
                .getattr(intern!(py, "semantic_hash"))?
                .cast_into::<PyString>()?
                .unbind(),
            py: e.clone().unbind(),
        });
        self.entries
            .borrow_mut()
            .insert(name, (e.clone().unbind(), entry.clone()));
        Ok(entry)
    }

    fn outputs_list<'py>(py: Python<'py>, entry: &Entry) -> PyResult<Bound<'py, PyList>> {
        let l = PyList::empty(py);
        for (o, _) in entry.outputs.iter().flatten() {
            l.append(o.bind(py))?;
        }
        Ok(l)
    }

    // ------------------------------------------------------------ start
    pub fn start_job(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        handle: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let shell = self.shell(py)?;
        if shell.getattr(intern!(py, "stopping"))?.is_truthy()? {
            return self.reject(py, item, "Cancelled", "the machine is stopping", handle, None);
        }
        let default_entry = self
            .h
            .machine
            .bind(py)
            .getattr(intern!(py, "default_entry"))?;
        let entry_of = |it: &Bound<'_, PyAny>| -> PyResult<Py<PyAny>> {
            let e = it.getattr(intern!(py, "entry"))?;
            Ok(if e.is_truthy()? {
                e.unbind()
            } else {
                default_entry.clone().unbind()
            })
        };
        let where_ = Core::where_entry(py, entry_of(item)?.bind(py))?;
        let kw = PyDict::new(py);
        kw.set_item("kind", self.h.source_item.bind(py))?;
        kw.set_item("where", where_)?;
        let got = self.h.plugins.bind(py).call_method(
            intern!(py, "transform"),
            ("pre_admit", item),
            Some(&kw),
        );
        let item = match got {
            Ok(i) => i,
            Err(e) if e.is_instance(py, self.h.plugin_block_error.bind(py)) => {
                let msg = e.value(py).str()?.to_string();
                return self.reject(py, item, "Blocked", &msg, handle, None);
            }
            Err(e) => return Err(e),
        };
        let key = item.getattr(intern!(py, "key"))?;
        if self.keys.bind(py).contains(&key)? {
            let msg = format!("job key {} is already running", key.repr()?);
            return self.reject(py, &item, "DuplicateKey", &msg, handle, None);
        }
        let name = entry_of(&item)?;
        let entry = self
            .h
            .code
            .bind(py)
            .getattr(intern!(py, "entries"))?
            .call_method1(intern!(py, "get"), (name.bind(py),))?;
        if entry.is_none() {
            let msg = format!("no entry {}", name.bind(py).repr()?);
            return self.reject(py, &item, "KirRuntimeError", &msg, handle, None);
        }
        let entry = self.entry_of(py, &entry)?;
        let name_s = name.bind(py).str()?.to_string();

        // Bind the entry's parameters; the top level takes no inputs. The inputs are any
        // mapping: ``sorted(set(inputs) - set(params))`` and ``in``/``[]`` as Python runs them
        let inputs = item.getattr(intern!(py, "inputs"))?;
        let builtins = py.import(intern!(py, "builtins"))?;
        let params = PySet::new(py, entry.params.iter().map(|p| p.py_name.bind(py)))?;
        let given = builtins.getattr(intern!(py, "set"))?.call1((&inputs,))?;
        let unknown = builtins
            .getattr(intern!(py, "sorted"))?
            .call1((given.sub(&params)?,))?;
        if unknown.is_truthy()? {
            let wh = if entry.outputs.is_some() {
                format!("@def {name_s}")
            } else {
                "the top level".into()
            };
            let msg = format!("{wh}: unknown inputs {}", unknown.str()?);
            return self.reject(py, &item, "KirRuntimeError", &msg, handle, Some(&name_s));
        }
        let mut names = NameMap::default();
        for p in &entry.params {
            let pn = p.py_name.bind(py);
            let given = match inputs.contains(pn)? {
                true => Some(inputs.get_item(pn)?),
                false => None,
            };
            match given {
                Some(v) => names.set(p.name, Slot::constant(v.unbind())),
                None if p.has_default => {
                    let d = p.default.bind(py);
                    let container = d.is_instance_of::<PyList>()
                        || d.is_instance_of::<PyDict>()
                        || d.is_instance_of::<pyo3::types::PySet>();
                    names.set(p.name, Slot::constant(self.fresh(py, &p.default, container)?));
                }
                None => {
                    let msg =
                        format!("@def {name_s}: input {} missing", p.py_name.bind(py).repr()?);
                    return self.reject(py, &item, "KirRuntimeError", &msg, handle, Some(&name_s));
                }
            }
        }

        // A job: one strand at the entry address; sampled for recording and for events
        let id = crate::runtime::state::next_id();
        let job = Job::new(id, key.clone().unbind(), entry.clone(), inputs.clone().unbind());
        let auto_key = item.getattr(intern!(py, "auto_key"))?.is_truthy()?;
        let observed = self
            .h
            .observe_execution
            .bind(py)
            .call1((
                entry.py.bind(py).getattr("region")?,
                self.h.code.bind(py),
                self.h.machine.bind(py),
            ))?
            .cast_into::<PyDict>()?;
        // The identity cache is borrowed read-only. Job stats escape in JobRecord,
        // so retain one detached admission copy rather than exposing that cache.
        let execution = self
            .h
            .deepcopy
            .bind(py)
            .call1((snapshot_field(&observed, "manifest")?,))?
            .unbind();
        let execution_epoch: i64 = snapshot_field(&observed, "epoch")?.extract()?;
        let execution_epoch = execution_epoch.into_pyobject(py)?.into_any().unbind();
        {
            let mut j = job.borrow_mut();
            j.start_time = self.perf();
            j.handle = handle.map(|h| h.clone().unbind());
            j.auto_key = auto_key;
            j.stats.program = Some(entry.region_hash.clone_ref(py));
            j.stats.extra.insert("execution".into(), execution);
            j.stats
                .extra
                .insert("execution_epoch".into(), execution_epoch);
        }
        let rate = self.h.knob(py, K::Record)?;
        let sampled = if rate.is_truthy()? {
            let r: f64 = self
                .h
                .record_rng
                .bind(py)
                .call_method0(intern!(py, "random"))?
                .extract()?;
            let recorded = r < crate::lanes::lane::as_f64(&rate)?;
            if recorded {
                let list = PyList::empty(py).unbind();
                job.borrow_mut().record = Some(list);
            }
            recorded
        } else {
            let r: f64 = self
                .h
                .event_rng
                .bind(py)
                .call_method0(intern!(py, "random"))?
                .extract()?;
            let sample = crate::lanes::lane::as_f64(&self.h.knob(py, K::EventSample)?)?;
            r < sample
        };
        job.borrow_mut().sampled = sampled;
        let st =
            Strand::new(&job, id, entry.addr, empty_prefix(), 0, no_frames(), Some(names), None);
        job.borrow_mut().add_strand(&st);

        // The inputs as given, before any statement runs: positional keys and snapshots
        if self.h.store.is_some() {
            let (dig, weak) = self.h.dg.digest_with_weak(inputs.as_any())?;
            if !weak.is_empty() {
                self.note_weak(py, &job, &weak)?;
            } else {
                job.borrow_mut().inputs_dig = Some(PyBytes::new(py, &dig).unbind());
            }
        }

        // A present incompatible candidate is not a cache miss: reject before admission
        // and before engines/on_job_start can repeat any external effects.
        let restored = match self.resume_snapshot(py, &job)? {
            SnapshotAdmission::New => false,
            SnapshotAdmission::Resumed => true,
            SnapshotAdmission::Reject(decision) => {
                return self.reject_snapshot(py, &item, decision.bind(py), handle, &name_s);
            }
        };

        // Region identity is local to this machine, never inherited from a snapshot.
        let region_index = entry
            .py
            .bind(py)
            .getattr(intern!(py, "region"))?
            .getattr(intern!(py, "index"))?;
        job.borrow_mut()
            .stats
            .extra
            .insert("program_region".into(), region_index.unbind());

        // Bytes held: the inputs for the job's life, restored values while their slots live
        let bytes = self.value_bytes(py, inputs.as_any())?;
        job.borrow_mut().bytes = bytes;
        if restored {
            let strands: Vec<_> = job.borrow().strands.values().cloned().collect();
            let param_ids: HashSet<u32> = entry.params.iter().map(|p| p.name).collect();
            for s in strands {
                let slots: Vec<_> = s
                    .borrow()
                    .names
                    .as_ref()
                    .map(|n| n.to_dict())
                    .unwrap_or_default()
                    .into_iter()
                    .collect();
                for (name, slot) in slots {
                    if param_ids.contains(&name) {
                        continue;
                    }
                    let v = slot
                        .borrow()
                        .value
                        .as_ref()
                        .map(|v| v.clone_ref(py))
                        .unwrap_or_else(|| py.None());
                    let size = self.value_bytes(py, v.bind(py))?;
                    let added = hold(&slot, id, size, &self.freed);
                    job.borrow_mut().bytes += added;
                }
            }
        }
        {
            let mut j = job.borrow_mut();
            j.stats.bytes_peak = j.stats.bytes_peak.max(j.bytes);
            self.pool_bytes.set(self.pool_bytes.get() + j.bytes);
        }
        self.jobs.borrow_mut().insert(id, job.clone());
        self.keys.bind(py).add(&key)?;
        self.mark_dirty(id);
        *self.last_started.borrow_mut() = Some(job.clone());
        self.h
            .bus
            .bind(py)
            .call_method1(intern!(py, "sample"), (id, sampled))?;
        if self.wants(py, "job.admit", Some(id))? {
            let f = PyDict::new(py);
            f.set_item("job", id)?;
            f.set_item("key", &key)?;
            f.set_item("entry", name.bind(py))?;
            self.emit(py, "job.admit", &f)?;
        }
        let info = PyDict::new(py);
        info.set_item("key", &key)?;
        info.set_item("entry", name.bind(py))?;
        info.set_item("inputs", &inputs)?;
        info.set_item("recorded", job.borrow().record.is_some())?;
        let kw = PyDict::new(py);
        kw.set_item("where", Core::where_entry(py, entry.py_name.bind(py))?)?;
        self.h.plugins.bind(py).call_method(
            intern!(py, "notify"),
            ("on_job_start", info),
            Some(&kw),
        )?;
        Ok(())
    }

    fn resume_snapshot(&self, py: Python<'_>, job: &JobRef) -> PyResult<SnapshotAdmission> {
        match self.resume_snapshot_inner(py, job) {
            Ok(outcome) => Ok(outcome),
            Err(error) if is_exception(py, &error) => {
                // This includes detached native conversion failures. The initial job
                // and interner remain untouched by restore until conversion succeeds.
                let issue = PyDict::new(py);
                issue.set_item("code", "invalid_snapshot")?;
                issue.set_item("path", "snapshot")?;
                issue.set_item(
                    "message",
                    format!("snapshot preparation failed: {}", error.value(py).str()?),
                )?;
                let decision = PyDict::new(py);
                decision.set_item("status", "invalid")?;
                decision.set_item("allow", false)?;
                decision.set_item("issues", PyList::new(py, [issue])?)?;
                Ok(SnapshotAdmission::Reject(decision.unbind()))
            }
            Err(error) => Err(error),
        }
    }

    fn resume_snapshot_inner(&self, py: Python<'_>, job: &JobRef) -> PyResult<SnapshotAdmission> {
        if !self.h.knob(py, K::SnapshotEvery)?.is_truthy()? {
            return Ok(SnapshotAdmission::New);
        }
        let Some(new_key) = self.snapshot_key(py, job)? else {
            return Ok(SnapshotAdmission::New);
        };
        let old_key = self
            .legacy_snapshot_key(py, job)?
            .ok_or_else(|| PyValueError::new_err("snapshot locator unavailable"))?;
        let store = self
            .h
            .store
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("snapshot store unavailable"))?;
        self.warn_auto_key(py, job);
        let errors = PyList::empty(py);
        let candidate = self
            .h
            .snapshot_candidate
            .bind(py)
            .call1((store.bind(py), new_key.bind(py), old_key.bind(py), errors.getattr("append")?))?
            .cast_into::<PyDict>()?;
        for error in errors.iter() {
            self.store_error(py, "get", &PyErr::from_value(error))?;
        }
        let action: String = snapshot_field(&candidate, "action")?.extract()?;
        match action.as_str() {
            "new" => return Ok(SnapshotAdmission::New),
            "reject" => {
                return Ok(SnapshotAdmission::Reject(
                    snapshot_field(&candidate, "decision")?
                        .cast_into::<PyDict>()?
                        .unbind(),
                ))
            }
            "candidate" => {}
            _ => return Err(PyValueError::new_err("invalid snapshot candidate action")),
        }
        let manifest = self.snapshot_execution(py, job)?;
        let entry = job.borrow().entry.py.clone_ref(py);
        let mode = self
            .h
            .machine
            .bind(py)
            .getattr("persistence_compatibility")?;
        let prepared = self
            .h
            .snapshot_prepare
            .bind(py)
            .call1((
                snapshot_field(&candidate, "blob")?,
                entry.bind(py),
                self.h.code.bind(py),
                manifest.bind(py),
                mode,
            ))?
            .cast_into::<PyDict>()?;
        let decision = snapshot_field(&prepared, "decision")?.cast_into::<PyDict>()?;
        if !snapshot_field(&decision, "allow")?.extract::<bool>()? {
            return Ok(SnapshotAdmission::Reject(decision.unbind()));
        }
        let state = snapshot_field(&prepared, "state")?.cast_into::<PyDict>()?;
        // Warnings/callbacks occur before native commit, never after partial application.
        let kwargs = PyDict::new(py);
        kwargs.set_item("execution_hash", manifest.bind(py).get_item("hash")?)?;
        self.h
            .warn_unverified
            .bind(py)
            .call((self.h.machine.bind(py), &decision), Some(&kwargs))?;
        self.apply_prepared_snapshot(py, job, &state, &decision)?;
        Ok(SnapshotAdmission::Resumed)
    }

    fn reject_snapshot(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        decision: &Bound<'_, PyDict>,
        handle: Option<&Bound<'_, PyAny>>,
        entry: &str,
    ) -> PyResult<()> {
        let rejected = self
            .h
            .snapshot_rejection
            .bind(py)
            .call1((decision,))?
            .cast_into::<PyDict>()?;
        let failure = snapshot_field(&rejected, "failure")?.cast_into::<PyDict>()?;
        let stats = snapshot_field(&rejected, "stats")?.cast_into::<PyDict>()?;
        let kind: String = snapshot_field(&failure, "kind")?.extract()?;
        let message: String = snapshot_field(&failure, "message")?.extract()?;
        self.reject_with_stats(py, item, &kind, &message, handle, Some(entry), Some(&stats))
    }

    /// Report once that keys are automatic while snapshots or positional stores are on.
    pub fn warn_auto_key(&self, py: Python<'_>, job: &JobRef) {
        if job.borrow().auto_key && !self.warned_keys.get() {
            self.warned_keys.set(true);
            let msg = "job keys are automatic (no key given) while snapshots or positional stores are on; a job is matched by key and inputs, so give explicit keys";
            self.h.log(py, "warning", msg, None);
        }
    }

    pub fn reject(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        kind: &str,
        msg: &str,
        handle: Option<&Bound<'_, PyAny>>,
        entry: Option<&str>,
    ) -> PyResult<()> {
        self.reject_with_stats(py, item, kind, msg, handle, entry, None)
    }

    #[allow(clippy::too_many_arguments)]
    fn reject_with_stats(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        kind: &str,
        msg: &str,
        handle: Option<&Bound<'_, PyAny>>,
        entry: Option<&str>,
        stats: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        let entry: Py<PyAny> = match entry {
            Some(e) => PyString::new(py, e).into_any().unbind(),
            None => {
                let e = item.getattr(intern!(py, "entry"))?;
                if e.is_truthy()? {
                    e.unbind()
                } else {
                    let d = self
                        .h
                        .machine
                        .bind(py)
                        .getattr(intern!(py, "default_entry"))?;
                    if d.is_truthy()? {
                        d.unbind()
                    } else {
                        PyString::new(py, "").into_any().unbind()
                    }
                }
            }
        };
        let failure = PyDict::new(py);
        failure.set_item("kind", kind)?;
        failure.set_item("node", py.None())?;
        failure.set_item("message", msg)?;
        let kwargs = PyDict::new(py);
        if let Some(stats) = stats {
            kwargs.set_item("stats", stats)?;
        }
        let rec = self.h.job_record.bind(py).call(
            (
                item.getattr(intern!(py, "key"))?,
                entry.bind(py),
                false,
                PyDict::new(py),
                failure,
            ),
            Some(&kwargs),
        )?;
        self.stats.borrow_mut().jobs_failed += 1;
        let kw = PyDict::new(py);
        kw.set_item("where", Core::where_entry(py, entry.bind(py))?)?;
        self.h.plugins.bind(py).call_method(
            intern!(py, "notify"),
            ("on_job_end", &rec),
            Some(&kw),
        )?;
        self.emit_record(py, &rec, handle)
    }

    // ------------------------------------------------------------ end
    /// End a job: its strands end, unfinished work is squashed, its record goes out.
    pub fn end_job(
        &self,
        py: Python<'_>,
        job: &JobRef,
        outputs: Bound<'_, PyDict>,
        missing: Bound<'_, PyList>,
    ) -> PyResult<()> {
        job.borrow_mut().ended = true;
        let strands: Vec<_> = job.borrow().strands.values().cloned().collect();
        for st in strands {
            job.borrow_mut().end_strand(&st);
        }
        let rest = job.borrow_mut().window.clear();
        for x in rest {
            let st = x.borrow().state;
            if !matches!(st, InfState::Done | InfState::Retired | InfState::Squashed) {
                self.squash(py, &x)?;
            } else {
                x.borrow_mut().release();
            }
        }
        {
            let mut j = job.borrow_mut();
            j.held.clear();
            j.memo = None;
            for collector in j.collectors.drain(..) {
                collector.borrow_mut().abort();
            }
        }
        self.release_freed();
        {
            let mut last = self.last_started.borrow_mut();
            if last.as_ref().is_some_and(|l| std::rc::Rc::ptr_eq(l, job)) {
                *last = None;
            }
        }

        // The record (built from copies: building it may run the collector)
        let (ok, jid, key, entry, failure, stats, handle, record, seconds, steps, bytes) = {
            let j = job.borrow();
            (
                j.failure.is_none(),
                j.id,
                j.key.clone_ref(py),
                j.entry.clone(),
                j.failure.as_ref().map(|f| f.clone_ref(py)),
                j.stats.snapshot(py),
                j.handle.as_ref().map(|h| h.clone_ref(py)),
                j.record.as_ref().map(|r| r.clone_ref(py)),
                self.perf() - j.start_time,
                j.steps,
                j.bytes,
            )
        };
        let stats = stats.to_dict(py)?;
        stats.set_item("seconds", seconds)?;
        stats.set_item("steps", steps)?;
        stats.set_item("bytes", bytes)?;
        let kw = PyDict::new(py);
        kw.set_item("record", record)?;
        let rec = self.h.job_record.bind(py).call(
            (
                key.bind(py),
                entry.py_name.bind(py),
                ok,
                &outputs,
                failure.as_ref().map(|f| f.bind(py)),
                &missing,
                &stats,
            ),
            Some(&kw),
        )?;

        // The pool forgets the job
        {
            let mut s = self.stats.borrow_mut();
            if ok {
                s.jobs_ok += 1;
            } else {
                s.jobs_failed += 1;
            }
        }
        self.jobs.borrow_mut().shift_remove(&jid);
        self.keys.bind(py).discard(key.bind(py))?;
        self.blocked.borrow_mut().remove(&jid);
        self.pool_bytes
            .set(self.pool_bytes.get() - job.borrow().bytes);

        // A finished job's snapshot (taken in this run or resumed from) is gone
        let has_snapshot = {
            let j = job.borrow();
            let truthy = |k: &str| {
                j.stats
                    .extra
                    .get(k)
                    .is_some_and(|v| v.bind(py).is_truthy().unwrap_or(false))
            };
            truthy("snapshots") || truthy("resumed")
        };
        if ok && has_snapshot {
            if let Some(snap) = self.snapshot_key(py, job)? {
                self.store_delete(py, &snap)?;
            }
        }

        // Event, plugins, then the handle or the sink
        if self.wants(py, "job.end", Some(jid))? {
            let f = PyDict::new(py);
            f.set_item("job", jid)?;
            f.set_item("key", key.bind(py))?;
            f.set_item("ok", ok)?;
            f.set_item("entry", entry.py_name.bind(py))?;
            f.set_item("failure", failure.as_ref().map(|x| x.bind(py)))?;
            f.set_item("stats", stats)?;
            self.emit(py, "job.end", &f)?;
        }
        self.h
            .bus
            .bind(py)
            .call_method1(intern!(py, "forget_job"), (jid,))?;
        let kw = PyDict::new(py);
        kw.set_item("where", Core::where_entry(py, entry.py_name.bind(py))?)?;
        self.h.plugins.bind(py).call_method(
            intern!(py, "notify"),
            ("on_job_end", &rec),
            Some(&kw),
        )?;
        self.emit_record(py, &rec, handle.as_ref().map(|h| h.bind(py)))
    }

    /// A failure inside the machine while working on one job: that job fails, the rest go on.
    pub fn job_error(&self, py: Python<'_>, job: &JobRef, e: &PyErr) -> PyResult<()> {
        let (key, ended, jid, entry) = {
            let j = job.borrow();
            (j.key.clone_ref(py), j.ended, j.id, j.entry.clone())
        };
        let key = key.bind(py).str()?.to_string();
        self.h
            .log(py, "error", &format!("job {key} failed inside the machine"), Some(e));
        self.stats.borrow_mut().machine_errors += 1;
        if ended || !self.jobs.borrow().contains_key(&jid) {
            return Ok(());
        }
        let t = e.get_type(py).name()?.to_string();
        let failure = PyDict::new(py);
        failure.set_item("kind", "MachineError")?;
        failure.set_item("node", py.None())?;
        failure.set_item("message", format!("{t}: {}", e.value(py).str()?))?;
        job.borrow_mut().failure = Some(failure.unbind());
        self.end_job(py, job, PyDict::new(py), Core::outputs_list(py, &entry)?)
    }

    pub fn cancel_job(&self, py: Python<'_>, job: &JobRef, message: &str) -> PyResult<()> {
        let failure = PyDict::new(py);
        failure.set_item("kind", "Cancelled")?;
        failure.set_item("node", py.None())?;
        failure.set_item("message", message)?;
        let entry = {
            let mut j = job.borrow_mut();
            j.failure = Some(failure.unbind());
            j.entry.clone()
        };
        self.end_job(py, job, PyDict::new(py), Core::outputs_list(py, &entry)?)
    }

    // ------------------------------------------------------------ records
    pub fn emit_record(
        &self,
        py: Python<'_>,
        rec: &Bound<'_, PyAny>,
        handle: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let kw = PyDict::new(py);
        kw.set_item("kind", self.h.job_record.bind(py))?;
        kw.set_item("where", Core::where_entry(py, &rec.getattr(intern!(py, "entry"))?)?)?;
        let got = self.h.plugins.bind(py).call_method(
            intern!(py, "transform"),
            ("pre_sink", rec),
            Some(&kw),
        );
        let out = match got {
            Ok(o) => o,
            Err(e) if e.is_instance(py, self.h.plugin_block_error.bind(py)) => {
                self.stats.borrow_mut().records_blocked += 1;
                if let Some(h) = handle {
                    let msg = e.value(py).str()?;
                    rec.getattr(intern!(py, "stats"))?
                        .set_item("sink_blocked", msg)?;
                    h.call_method1(intern!(py, "_set"), (rec,))?;
                }
                return Ok(());
            }
            Err(e) => return Err(e),
        };
        if let Some(h) = handle {
            h.call_method1(intern!(py, "_set"), (out,))?;
            return Ok(());
        }
        let shell = self.shell(py)?;
        if !shell.getattr(intern!(py, "sink_q"))?.is_none() {
            shell.call_method1(intern!(py, "to_sink"), (out,))?;
        } else {
            self.stats.borrow_mut().records_dropped += 1;
        }
        Ok(())
    }

    // ------------------------------------------------------------ bytes
    /// Apply the bytes released by freed held slots to their jobs and the pool.
    pub fn release_freed(&self) {
        loop {
            let next = self.freed.borrow_mut().pop_front();
            let Some((jid, size)) = next else { break };
            let job = self.job(jid);
            if let Some(job) = job {
                let mut j = job.borrow_mut();
                if !j.ended {
                    j.bytes -= size;
                    self.pool_bytes.set(self.pool_bytes.get() - size);
                }
            }
        }
    }
}
