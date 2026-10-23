//! Writeback: results fill slots and wake waiters (spec/03 §3.8).

use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple,
};

use crate::program::core::StoreMode;
use crate::runtime::core::Core;
use crate::runtime::events::{Exc, Res};
use crate::runtime::host::{is_exception, K};
use crate::runtime::python::Owner;
use crate::runtime::state::slot::hold;
use crate::runtime::state::{
    deref, InfRef, InfState, Obj, SlotRef, SlotState, StrandState, Waiter,
};

impl Core {
    // ------------------------------------------------------------ slots and waking
    pub fn wake(&self, py: Option<Python<'_>>, w: Waiter) -> PyResult<()> {
        match w {
            Waiter::Strand(st) => {
                let (jid, sid, pc) = {
                    let mut s = st.borrow_mut();
                    if !matches!(s.state, StrandState::Parked | StrandState::Stalled) {
                        return Ok(());
                    }
                    s.state = StrandState::Runnable;
                    (s.job_id, s.id, s.pc)
                };
                let job = st.borrow().job.upgrade();
                if let Some(job) = job {
                    job.borrow_mut().mark_runnable(&st);
                }
                self.mark_dirty(jid);
                self.note_strand(py, "strand.resume", jid, sid, pc, None)
            }
            Waiter::Inf(inf) => {
                if inf.borrow().state == InfState::Waiting {
                    self.check(py, &inf)?;
                }
                Ok(())
            }
        }
    }

    pub fn fill(&self, py: Python<'_>, slot: &SlotRef, value: Obj) -> PyResult<()> {
        let waiters = {
            let mut s = slot.borrow_mut();
            s.value = Some(std::rc::Rc::new(value));
            s.state = SlotState::Ready;
            std::mem::take(&mut s.waiters)
        };
        for w in waiters {
            self.wake(Some(py), w)?;
        }
        Ok(())
    }

    pub fn resolve_phi(
        &self,
        py: Option<Python<'_>>,
        phi: &SlotRef,
        target: &SlotRef,
    ) -> PyResult<()> {
        phi.borrow_mut().forward = Some(target.clone());
        let t = deref(target);

        // Pending `mutates` versions and waiters move to the resolved slot
        let (muts, waiters) = {
            let mut p = phi.borrow_mut();
            (std::mem::take(&mut p.muts), std::mem::take(&mut p.waiters))
        };
        if !muts.is_empty() {
            t.borrow_mut().muts.extend(muts);
        }
        if t.borrow().state != SlotState::Pending {
            for w in waiters {
                self.wake(py, w)?;
            }
        } else {
            t.borrow_mut().waiters.extend(waiters);
        }
        Ok(())
    }

    /// Estimated bytes held by a value (`togeari.core.runtime.values.value_bytes`).
    pub fn value_bytes(&self, py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<i64> {
        if v.is_none()
            || v.is_instance_of::<PyBool>()
            || v.is_instance_of::<PyInt>()
            || v.is_instance_of::<PyFloat>()
        {
            return Ok(0);
        }
        if let Ok(s) = v.cast::<PyString>() {
            return Ok(s.len()? as i64);
        }
        if let Ok(b) = v.cast::<PyBytes>() {
            return Ok(b.as_bytes().len() as i64);
        }
        if let Ok(b) = v.cast::<PyByteArray>() {
            return Ok(b.len() as i64);
        }
        self.h.value_bytes.bind(py).call1((v,))?.extract()
    }

    // ------------------------------------------------------------ lane hooks
    /// `pre_submit` plugin hook; blocked items fail as `ItemError(Blocked)`.
    pub fn before_submit<'py>(
        &self,
        py: Python<'py>,
        lane: &Bound<'py, PyAny>,
        unit: &Bound<'py, PyAny>,
        items: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if !self.h.has_hook(py, "pre_submit")? {
            return Ok(items);
        }
        let kept = PyList::empty(py);
        let lane_name = lane.getattr(intern!(py, "name"))?;
        let kinds = self.h.work_item.bind(py);
        for it in items.try_iter()? {
            let it = it?;
            let owner = it.getattr(intern!(py, "owner"))?;
            let identity = if owner.is_none() {
                PyTuple::new(
                    py,
                    [
                        it.getattr("job_execution_id")?,
                        it.getattr("invocation_id")?,
                        it.getattr("attempt_id")?,
                    ],
                )?
            } else {
                owner
                    .call_method0("_identity_fields")?
                    .cast_into::<PyTuple>()?
            };
            let where_ = PyDict::new(py);
            where_.set_item("lane", &lane_name)?;
            let entry = if owner.is_none() {
                py.None().into_bound(py)
            } else {
                owner.getattr(intern!(py, "entry"))?
            };
            where_.set_item("entry", entry)?;
            let kw = PyDict::new(py);
            kw.set_item("kind", kinds)?;
            kw.set_item("where", where_)?;
            kw.set_item("unit", unit)?;
            kw.set_item("lane", &lane_name)?;
            let got = self.h.plugins.bind(py).call_method(
                intern!(py, "transform"),
                ("pre_submit", &it),
                Some(&kw),
            );
            let new = match got {
                Ok(n) => n,
                Err(e) if e.is_instance(py, self.h.plugin_block_error.bind(py)) => {
                    it.setattr(intern!(py, "owner"), &owner)?;
                    it.call_method("_set_identity", &identity, None)?;
                    it.getattr(intern!(py, "ext"))?.set_item("settled", true)?;
                    let msg = e.value(py).str()?;
                    let err = self.h.blocked.bind(py).call1((msg,))?;
                    let r = self.h.item_error.bind(py).call1((err,))?;
                    self.deliver(py, &it, &r, None)?;
                    continue;
                }
                Err(e) => return Err(e),
            };

            // The captured owner/identity wins over same-object or replacement spoofing.
            it.setattr(intern!(py, "owner"), &owner)?;
            it.call_method("_set_identity", &identity, None)?;
            if !new.get_type().is(kinds) {
                it.getattr(intern!(py, "ext"))?.set_item("settled", true)?;
                let error = self.h.blocked.bind(py).call1((
                    "pre_submit replacement must be an exact WorkItem; use ext for plugin data",
                ))?;
                let result = self.h.item_error.bind(py).call1((error,))?;
                self.deliver(py, &it, &result, None)?;
                continue;
            }
            new.setattr(intern!(py, "owner"), &owner)?;
            if !new.is(&it) {
                py.import("togeari.core.lanes.deadline")?
                    .call_method1("replace", (&it, &new))?;
                if let Some(inf) = self.owned_inf(&owner)? {
                    inf.borrow_mut().item = Some(new.clone().unbind());
                }
            }
            new.call_method("_set_identity", &identity, None)?;
            kept.append(new)?;
        }
        Ok(kept.into_any())
    }

    /// The live inflight an item's owner names, if it is still held.
    pub fn owned_inf(&self, owner: &Bound<'_, PyAny>) -> PyResult<Option<InfRef>> {
        let Ok(o) = owner.cast::<Owner>() else {
            return Ok(None);
        };
        Ok(self
            .owned
            .borrow()
            .get(&o.get().inf)
            .and_then(|w| w.upgrade()))
    }

    // ------------------------------------------------------------ completions
    pub fn deliver(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        result: &Bound<'_, PyAny>,
        unit: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let owner = item.getattr(intern!(py, "owner"))?;
        let Ok(own) = owner.cast::<Owner>() else {
            return Ok(());
        };
        let own = own.get();
        let jid = own.job_id;

        // The result event, with queue and service times
        if self.wants(py, "lane.result", Some(jid))? {
            let ext = item.getattr(intern!(py, "ext"))?;
            let sent = ext.call_method1(intern!(py, "get"), ("sent",))?;
            let has_sent = sent.is_truthy()?;
            let queued = if has_sent && ext.contains("enqueued")? {
                Some(sent.sub(ext.get_item("enqueued")?)?)
            } else {
                None
            };
            let service = if has_sent {
                Some(PyFloat::new(py, self.now(py)?).into_any().sub(&sent)?)
            } else {
                None
            };
            let f = PyDict::new(py);
            f.set_item("job", jid)?;
            f.set_item("node", item.getattr(intern!(py, "node"))?)?;
            f.set_item("addr", own.instr.bind(py).getattr(intern!(py, "addr"))?)?;
            f.set_item("lane", own.lane.bind(py))?;
            f.set_item("unit", ext.call_method1(intern!(py, "get"), ("unit",))?)?;
            f.set_item("queued_s", queued)?;
            f.set_item("service_s", service)?;
            f.set_item("result", result.get_type().name()?)?;
            let policy = ext.call_method1(intern!(py, "get"), ("policy_failure",))?;
            if !policy.is_none() {
                f.set_item("policy_failure", policy.cast::<PyDict>()?.copy()?)?;
            }
            self.emit(py, "lane.result", &f)?;
        }

        // A cancel the machine did not ask for fails the statement
        let cancelled = result.is_instance(self.h.cancelled.bind(py))?;
        let Some(inf) = self.owned_inf(&owner)? else {
            // The inflight is gone: squashed, retired, or its job ended
            let squashed = own.fate() == Some("squashed");
            let resident = self.jobs.borrow().contains_key(&jid);
            if !(cancelled && squashed) && (squashed || !resident) {
                self.stats.borrow_mut().wasted += 1;
            }
            return Ok(());
        };
        let job = inf.borrow().job.upgrade();
        let r = if cancelled {
            if inf.borrow().state == InfState::Squashed {
                return Ok(());
            }
            let node = item.getattr(intern!(py, "node"))?;
            let err = PyRuntimeError::new_err(format!("{node}: cancelled"));
            let res = Res::ItemError(err.value(py).clone().into_any().unbind());
            self.complete(py, &inf, res, None, unit)
        } else {
            let res = self.res_of(result)?;
            self.complete(py, &inf, res, Some(result.clone().unbind()), unit)
        };
        match r {
            Ok(()) => Ok(()),
            Err(e) if is_exception(py, &e) => match job {
                Some(job) => self.job_error(py, &job, &e),
                None => {
                    // The job already ended: the error is logged and counted only
                    let ext = item.getattr(intern!(py, "ext"))?;
                    let key = ext.call_method1(intern!(py, "get"), ("job_key",))?;
                    let msg = format!("job {} failed inside the machine", key.str()?);
                    self.h.log(py, "error", &msg, Some(&e));
                    self.stats.borrow_mut().machine_errors += 1;
                    Ok(())
                }
            },
            Err(e) => Err(e),
        }
    }

    /// Writeback: fill output slots and versions, or take the item's failure. ``orig`` is the
    /// result object as the engine gave it (``post_result`` receives it).
    pub fn complete(
        &self,
        py: Python<'_>,
        inf: &InfRef,
        res: Res,
        orig: Option<Obj>,
        unit: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let (state, job) = {
            let x = inf.borrow();
            (x.state, x.job.upgrade())
        };
        let ended = job.as_ref().is_none_or(|j| j.borrow().ended);
        if state == InfState::Squashed || ended {
            self.stats.borrow_mut().wasted += 1;
            return Ok(());
        }
        if !matches!(state, InfState::Issued | InfState::Ready) {
            return Ok(());
        }
        let job = job.expect("a resident job");
        let ins = inf.borrow().ins.clone();
        let mut res = res;
        if self.h.has_hook(py, "post_result")? {
            let lane = match &ins.binding {
                Some(b) if !b.inline => b.lane_name.as_ref().map(|l| l.clone_ref(py).into_any()),
                _ => None,
            };
            let (entry, jkey) = {
                let j = job.borrow();
                (j.entry.py_name.clone_ref(py), j.key.clone_ref(py))
            };
            let where_ = PyDict::new(py);
            where_.set_item("entry", entry)?;
            where_.set_item("lane", lane)?;
            let h = &self.h;
            let kinds = PyTuple::new(
                py,
                [
                    h.ok.bind(py),
                    h.item_error.bind(py),
                    h.unit_error.bind(py),
                    h.cancelled.bind(py),
                ],
            )?;
            let kw = PyDict::new(py);
            kw.set_item("kind", kinds)?;
            kw.set_item("where", where_)?;
            kw.set_item("node", ins.node.bind(py))?;
            kw.set_item("job", jkey)?;
            let current = match orig {
                Some(o) => o,
                None => self.res_to_py(py, &res)?,
            };
            let new = h.plugins.bind(py).call_method(
                intern!(py, "transform"),
                ("post_result", current),
                Some(&kw),
            )?;
            res = self.res_of(&new)?;
        }
        let jid = job.borrow().id;
        self.mark_dirty(jid);

        // The item's failure, or its value
        let node = ins.node.bind(py).to_string();
        let (mut value, finished) = match res {
            Res::ItemError(e) | Res::UnitError(e) => return self.fault(py, inf, Exc::Obj(e), true),
            Res::Cancelled => {
                return self.fault(
                    py,
                    inf,
                    Exc::Named("RuntimeError", format!("{node}: cancelled")),
                    true,
                )
            }
            Res::Ok(v, finished) => (v, finished),
            Res::Other(o) => {
                let msg = format!("{node}: result {}", o.bind(py).repr()?);
                return self.fault(py, inf, Exc::Named("TypeError", msg), true);
            }
        };

        // Decode the raw reply when the engine did not
        let item = inf.borrow().item.as_ref().map(|i| i.clone_ref(py));
        if !finished {
            if let Some(item) = item {
                match item
                    .bind(py)
                    .call_method1(intern!(py, "finish"), (value.bind(py),))
                {
                    Ok(v) => value = v.unbind(),
                    Err(e) if is_exception(py, &e) => {
                        return self.fault(
                            py,
                            inf,
                            Exc::Obj(e.value(py).clone().into_any().unbind()),
                            true,
                        )
                    }
                    Err(e) => return Err(e),
                }
            }
        }

        // Debug check: a node without `mutates` left its inputs unchanged
        let dig = inf.borrow().dig.as_ref().map(|d| d.clone_ref(py));
        if let Some(dig) = dig {
            if ins.mutates.is_empty() && self.h.knob(py, K::FingerprintInputs)?.is_truthy()? {
                let values = inf.borrow().values.as_ref().expect("values").clone_ref(py);
                let now: String = self
                    .h
                    .dg
                    .digest(values.bind(py).as_any())?
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect();
                if !dig.bind(py).eq(now)? {
                    let msg = format!("{node} changed an input");
                    return self.fault(py, inf, Exc::Named("UndeclaredMutation", msg), true);
                }
            }
        }
        if self.bind_outputs(py, inf, &value, unit)? {
            self.settle(py, inf, value)?;
        }
        Ok(())
    }

    /// Fill output slots like the reference interpreter, then `mutates` versions; False after a
    /// fault on an output count mismatch.
    fn bind_outputs(
        &self,
        py: Python<'_>,
        inf: &InfRef,
        value: &Obj,
        unit: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let ins = inf.borrow().ins.clone();
        let v = value.bind(py);
        if !ins.outs.is_empty() {
            let vals: Vec<Obj> = if ins.n_sig_out == 1 || ins.outs.len() == 1 {
                vec![value.clone_ref(py)]
            } else {
                let vals = match py.get_type::<PyList>().call1((v,)) {
                    Ok(l) => l
                        .try_iter()?
                        .map(|x| x.map(|x| x.unbind()))
                        .collect::<PyResult<Vec<_>>>()?,
                    Err(e) if e.is_instance_of::<PyTypeError>(py) => Vec::new(),
                    Err(e) => return Err(e),
                };
                if vals.len() != ins.outs.len() {
                    let msg = format!(
                        "{} returned {} values for {} outputs",
                        ins.node.bind(py),
                        vals.len(),
                        ins.outs.len()
                    );
                    self.fault(py, inf, Exc::Runtime(msg), false)?;
                    return Ok(false);
                }
                vals
            };
            let job = inf.borrow().job.upgrade().expect("a resident job");
            let jid = job.borrow().id;
            let outs: Vec<Option<SlotRef>> = inf.borrow().outs.clone();
            let mut held = 0;
            for (slot, val) in outs.into_iter().zip(vals) {
                let Some(slot) = slot else { continue };
                let size = self.value_bytes(py, val.bind(py))?;
                self.fill(py, &slot, val)?;
                let added = hold(&slot, jid, size, &self.freed);
                job.borrow_mut().bytes += added;
                held += added;
            }

            // Bytes the filled slots hold count toward the job and the pool while they live
            if held != 0 {
                self.pool_bytes.set(self.pool_bytes.get() + held);
                job.borrow_mut().stats.bytes_produced += held;
                if !self.freed.borrow().is_empty() {
                    self.release_freed();
                }
                let mut j = job.borrow_mut();
                if j.bytes > j.stats.bytes_peak {
                    j.stats.bytes_peak = j.bytes;
                }
            }
        }

        // `mutates` versions: the changed object (or the returned one from a copying unit)
        let has_versions = inf.borrow().versions.is_some();
        if has_versions {
            let shares = match &ins.binding {
                Some(b) if b.inline => true,
                _ => match unit {
                    Some(u) => match u.getattr_opt(intern!(py, "shares_memory"))? {
                        Some(s) => s.is_truthy()?,
                        None => true,
                    },
                    None => true,
                },
            };
            let fills: Vec<(SlotRef, Py<PyString>, usize)> = {
                let x = inf.borrow();
                x.versions
                    .iter()
                    .flatten()
                    .map(|ver| (ver.version.clone(), ins.mutates[ver.k].clone_ref(py), ver.k))
                    .collect()
            };
            let values = inf.borrow().values.as_ref().expect("values").clone_ref(py);
            for (version, port, k) in fills {
                let out = ins.mutates_out[k];
                let new = match out {
                    Some(j) if !shares => {
                        if ins.sig_outputs.len() == 1 {
                            value.clone_ref(py)
                        } else {
                            py.get_type::<PyList>().call1((v,))?.get_item(j)?.unbind()
                        }
                    }
                    _ => values
                        .bind(py)
                        .get_item(port.bind(py))?
                        .expect("a mutated input")
                        .unbind(),
                };
                self.fill(py, &version, new)?;
            }
        }
        Ok(true)
    }

    /// A bound statement is done: record and store copies, counters, store, event.
    fn settle(&self, py: Python<'_>, inf: &InfRef, value: Obj) -> PyResult<()> {
        let job = inf.borrow().job.upgrade().expect("a resident job");
        let ins = inf.borrow().ins.clone();
        let store = ins
            .binding
            .as_ref()
            .map(|b| b.store)
            .unwrap_or(StoreMode::No);

        // Recorded jobs, and positional results stored at retire, keep copies no later in-place
        // change reaches; an uncopyable positional result is not stored
        inf.borrow_mut().result = Some(value.clone_ref(py));
        let positional = {
            let x = inf.borrow();
            x.store_key.is_some() && !x.hit && store == StoreMode::Positional
        };
        let recorded = job.borrow().record.is_some();
        if recorded || positional {
            let got = self.h.kept_copy.bind(py).call1((value.bind(py),))?;
            let kept = got.get_item(0)?.unbind();
            let ok = got.get_item(1)?.is_truthy()?;
            inf.borrow_mut().kept = Some(kept);
            if !ok && recorded {
                let mut j = job.borrow_mut();
                let n = j.stats.get_int(py, "record_uncopied");
                j.stats.extra.insert(
                    "record_uncopied".into(),
                    (n + 1).into_pyobject(py)?.into_any().unbind(),
                );
            }
            if !ok && positional {
                inf.borrow_mut().store_key = None;
                self.count_extra("store_uncopied");
            }
            let has_versions = inf.borrow().versions.is_some();
            if has_versions && recorded {
                let mutated = PyDict::new(py);
                let values = inf.borrow().values.as_ref().expect("values").clone_ref(py);
                let ports: Vec<Py<PyString>> = inf
                    .borrow()
                    .versions
                    .iter()
                    .flatten()
                    .map(|v| ins.mutates[v.k].clone_ref(py))
                    .collect();
                for p in ports {
                    let v = values
                        .bind(py)
                        .get_item(p.bind(py))?
                        .expect("a mutated input");
                    let c = self.h.kept_copy.bind(py).call1((v,))?.get_item(0)?;
                    mutated.set_item(p.bind(py), c)?;
                }
                inf.borrow_mut().mutated = Some(mutated.into_any().unbind());
            }
        }

        // The work item lets go of its inputs and built payload
        let item = inf.borrow().item.as_ref().map(|i| i.clone_ref(py));
        if let Some(it) = &item {
            it.bind(py).call_method0(intern!(py, "release"))?;
        }

        {
            let mut x = inf.borrow_mut();
            let mut j = job.borrow_mut();
            if x.state == InfState::Issued {
                j.issued -= 1;
            }
            x.state = InfState::Done;
            x.t_done = self.perf();
            j.unfinished -= 1;
        }
        let (put, hit, reused) = {
            let x = inf.borrow();
            let put = match &x.store_key {
                Some(k) if !x.hit && store == StoreMode::Content => Some(k.clone_ref(py)),
                _ => None,
            };
            (put, x.hit, x.reused)
        };
        if let Some(k) = put {
            if self.result_store_allowed(py, &job, &ins)? {
                self.store_put(py, &k, value.bind(py))?;
            }
        }
        let jid = job.borrow().id;
        if self.wants(py, "stmt.done", Some(jid))? {
            let where_: Obj = match &item {
                Some(it) => it
                    .bind(py)
                    .getattr(intern!(py, "ext"))?
                    .call_method1(intern!(py, "get"), ("unit",))?
                    .unbind(),
                None if hit => PyString::new(py, "store").into_any().unbind(),
                None if reused => PyString::new(py, "reused").into_any().unbind(),
                None => PyString::new(py, "inline").into_any().unbind(),
            };
            self.emit_stmt(py, "stmt.done", inf, Some(("unit", where_)))?;
        }
        Ok(())
    }
}
