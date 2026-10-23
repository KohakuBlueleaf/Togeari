//! Precise faults: mark, squash everything younger, take in program order (spec/03 §3.9).
//! Completed calls a squash drops keep their results for their refetch (``squash_reuse``).

use std::rc::Rc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};

use crate::lanes::lane::CANCEL;
use crate::program::core::{Op, StoreMode};
use crate::runtime::core::Core;
use crate::runtime::events::{Exc, Res};
use crate::runtime::host::K;
use crate::runtime::state::{
    effective, Fault, FrameKind, InfRef, InfState, JobRef, Key, Memo, NameMap, Slot, SlotState,
    Snap, Strand,
};

impl Core {
    /// ``(kind, message, runtime)`` of a fault's error.
    fn describe(&self, py: Python<'_>, exc: &Exc) -> PyResult<(String, String, bool)> {
        Ok(match exc {
            Exc::Runtime(m) => ("KirRuntimeError".into(), m.clone(), true),
            Exc::Named(k, m) => (k.to_string(), m.clone(), false),
            Exc::Obj(o) => {
                let o = o.bind(py);
                let msg = o.str()?.to_string();
                if o.is_instance(self.h.runtime_fault.bind(py))? {
                    ("KirRuntimeError".into(), msg, true)
                } else {
                    (o.get_type().name()?.to_string(), msg, false)
                }
            }
        })
    }

    pub fn fault(&self, py: Python<'_>, inf: &InfRef, exc: Exc, catchable: bool) -> PyResult<()> {
        let (kind, message, runtime) = self.describe(py, &exc)?;
        let job = inf.borrow().job.upgrade().expect("a resident job");
        let node = inf.borrow().ins.node.clone_ref(py);
        let error = PyDict::new(py);
        error.set_item("kind", kind)?;
        error.set_item("node", node)?;
        error.set_item("message", message)?;
        let (key, handled) = {
            let mut x = inf.borrow_mut();
            let mut j = job.borrow_mut();
            if x.state.live() && !x.control {
                j.unfinished -= 1;
            }
            if x.state == InfState::Issued {
                j.issued -= 1;
            }
            let catchable = catchable && !runtime;
            x.fault = Some(Fault { error: error.unbind(), catchable });
            x.state = InfState::Done;

            // Only a fault a handler takes refetches the squashed code; others end the job
            (x.key.clone(), catchable && x.ins.try_id.is_some())
        };
        let jid = job.borrow().id;
        self.mark_dirty(jid);
        self.squash_younger(py, &job, &key, handled)
    }

    /// Squash every younger statement and strand; with ``keep``, completed results stay for the
    /// refetch (``squash_reuse``).
    pub fn squash_younger(
        &self,
        py: Python<'_>,
        job: &JobRef,
        key: &Key,
        keep: bool,
    ) -> PyResult<()> {
        self.checkpoint_fault(py, job, key)?;
        let cap = if keep {
            self.h.knob_f64(py, K::SquashReuse)?.unwrap_or(0.0)
        } else {
            0.0
        };
        let cut = job.borrow_mut().window.cut_after(key);
        for x in cut {
            if cap != 0.0 {
                let cap_bytes = self.h.knob_f64(py, K::SquashReuseBytes)?;
                self.keep_for_reuse(py, job, &x, cap, cap_bytes)?;
            }
            self.squash(py, &x)?;
        }
        let strands: Vec<_> = job.borrow().strands.values().cloned().collect();
        for st in strands {
            if st.borrow().next_key() > *key {
                job.borrow_mut().end_strand(&st);
            }
        }

        // A younger strand that already reached the end no longer counts
        let mut j = job.borrow_mut();
        if j.final_key.as_ref().is_some_and(|k| k > key) {
            j.final_names = None;
            j.final_key = None;
        }
        Ok(())
    }

    /// Keep the result of a completed call the squash drops, by address and inputs digest, for
    /// its refetch; at most ``cap`` entries and ``cap_bytes`` bytes per job, oldest dropped
    /// first. Kept: calls without `ordered` or `mutates` whose binding is not positional.
    fn keep_for_reuse(
        &self,
        py: Python<'_>,
        job: &JobRef,
        x: &InfRef,
        cap: f64,
        cap_bytes: Option<f64>,
    ) -> PyResult<()> {
        let (result, values, addr) = {
            let b = x.borrow();
            let ins = &b.ins;
            let reusable = b.state == InfState::Done
                && b.fault.is_none()
                && ins.op == Op::Call
                && !ins.ordered
                && ins.mutates.is_empty()
                && ins
                    .binding
                    .as_ref()
                    .is_some_and(|binding| binding.deterministic)
                && ins.binding.as_ref().map(|bd| bd.store) != Some(StoreMode::Positional)
                && b.values.is_some();
            if !reusable {
                return Ok(());
            }
            let result = b
                .result
                .as_ref()
                .map(|r| r.clone_ref(py))
                .unwrap_or_else(|| py.None());
            (result, b.values.as_ref().expect("values").clone_ref(py), ins.addr)
        };
        if !self.memo_execution_matches(py, job, addr)? {
            return Ok(());
        }
        let size = self.value_bytes(py, result.bind(py))?;
        if cap_bytes.is_some_and(|c| size as f64 > c) {
            return Ok(());
        }
        let (dig, weak) = self.h.dg.digest_with_weak(values.bind(py).as_any())?;
        if !weak.is_empty() || !self.memo_execution_matches(py, job, addr)? {
            return Ok(());
        }
        let dig = PyBytes::new(py, &dig).unbind();

        // The entry, and its bytes on the job until it is used or dropped
        {
            let mut j = job.borrow_mut();
            j.memo
                .get_or_insert_with(Memo::default)
                .add(addr, (dig, result, size));
            j.bytes += size;
        }
        self.pool_bytes.set(self.pool_bytes.get() + size);

        // Over a cap: the oldest address's oldest entry goes
        loop {
            let dropped = {
                let mut j = job.borrow_mut();
                let memo = j.memo.as_mut().expect("a memo");
                if memo.n as f64 > cap || cap_bytes.is_some_and(|c| memo.bytes as f64 > c) {
                    Some(memo.pop_oldest())
                } else {
                    None
                }
            };
            match dropped {
                Some(size) => self.memo_dropped(job, size),
                None => break,
            }
        }
        Ok(())
    }

    fn memo_execution_matches(&self, py: Python<'_>, job: &JobRef, addr: i64) -> PyResult<bool> {
        let region = self.h.code.bind(py).call_method1("region_of", (addr,))?;
        let stats = PyDict::new(py);
        {
            let job = job.borrow();
            for key in ["execution", "execution_epoch"] {
                if let Some(value) = job.stats.extra.get(key) {
                    stats.set_item(key, value.bind(py))?;
                }
            }
        }
        self.h
            .memo_execution
            .bind(py)
            .call1((region, self.h.code.bind(py), self.h.machine.bind(py), stats))?
            .extract()
    }

    fn memo_dropped(&self, job: &JobRef, size: i64) {
        job.borrow_mut().bytes -= size;
        self.pool_bytes.set(self.pool_bytes.get() - size);
    }

    /// Complete ``inf`` with a kept result of the same statement and inputs; False if none.
    pub fn take_reused(
        &self,
        py: Python<'_>,
        job: &JobRef,
        inf: &InfRef,
        dig: &[u8],
    ) -> PyResult<bool> {
        let addr = {
            let inf = inf.borrow();
            if !inf
                .ins
                .binding
                .as_ref()
                .is_some_and(|binding| binding.deterministic)
                || inf.ins.ordered
                || !inf.ins.mutates.is_empty()
            {
                return Ok(false);
            }
            inf.ins.addr
        };
        if !self.memo_execution_matches(py, job, addr)? {
            return Ok(false);
        }
        let found = job
            .borrow_mut()
            .memo
            .as_mut()
            .and_then(|m| m.take(py, addr, dig));
        let Some((value, size)) = found else {
            return Ok(false);
        };

        // The entry left the memo; its value goes through writeback like a store hit
        self.memo_dropped(job, size);
        {
            let mut inf = inf.borrow_mut();
            inf.reused = true;
            inf.resolution = "squash_reuse";
        }
        job.borrow_mut().stats.reused += 1;
        self.stats.borrow_mut().reused += 1;
        job.borrow_mut().issue(inf);
        self.complete(py, inf, Res::Ok(value, true), None, None)?;
        Ok(true)
    }

    /// Squash one inflight: counts, its lane item, its output and version slots; it lets go of
    /// its slots and values.
    pub fn squash(&self, py: Python<'_>, x: &InfRef) -> PyResult<()> {
        let job = x.borrow().job.upgrade().expect("a resident job");
        let (item, lane) = {
            let b = x.borrow();
            let mut j = job.borrow_mut();
            if b.state.live() && !b.control {
                j.unfinished -= 1;
            }
            if b.state == InfState::Issued {
                j.issued -= 1;
            }
            let lane = match &b.ins.binding {
                Some(bd) if !bd.inline => bd.lane.as_ref().map(|l| l.clone_ref(py)),
                _ => None,
            };
            (b.item.as_ref().map(|i| i.clone_ref(py)), lane)
        };

        // Its lane item is dropped from the queue, or its submission cancelled
        if let (Some(item), Some(lane)) = (item, lane) {
            match self.native_for(py, lane.bind(py), CANCEL)? {
                Some(n) => n.get().c().cancel(py, item.bind(py))?,
                None => {
                    lane.bind(py).call_method1(intern!(py, "cancel"), (item,))?;
                }
            }
        }

        // Its output and version slots are squashed
        {
            let mut b = x.borrow_mut();
            b.state = InfState::Squashed;
            if b.collect_index.is_none() {
                if let Some(c) = &b.collector {
                    c.borrow_mut().queued = false;
                }
            }
            for s in b.outs.iter().flatten() {
                s.borrow_mut().state = SlotState::Squashed;
            }
            for v in b.versions.iter().flatten() {
                v.version.borrow_mut().state = SlotState::Squashed;
            }
        }
        job.borrow_mut().stats.squashed += 1;
        self.stats.borrow_mut().squashed += 1;
        let jid = job.borrow().id;
        if self.wants(py, "stmt.squash", Some(jid))? {
            self.emit_stmt(py, "stmt.squash", x, None)?;
        }
        self.disown(x, "squashed");
        x.borrow_mut().release();
        Ok(())
    }

    /// An inflight leaves the work-item registry; its owner keeps its fate (no Python call).
    pub fn disown(&self, x: &InfRef, fate: &str) {
        let b = x.borrow();
        if let Some(o) = &b.owner {
            o.get().set_fate(fate);
            self.owned.borrow_mut().remove(&b.id);
        }
    }

    pub fn take_fault(&self, py: Python<'_>, job: &JobRef, x: &InfRef) -> PyResult<()> {
        job.borrow_mut().window.pop_first();
        x.borrow_mut().state = InfState::Retired;
        job.borrow_mut().checkpoint_watermark = Some(x.borrow().key.clone());
        self.disown(x, "retired");
        let (err, control, ins, key, dig) = {
            let b = x.borrow();
            let f = b.fault.as_ref().expect("a fault");
            (
                f.error.clone_ref(py),
                b.control,
                b.ins.clone(),
                b.key.clone(),
                b.dig.as_ref().map(|d| d.clone_ref(py)),
            )
        };
        let record = job.borrow().record.as_ref().map(|r| r.clone_ref(py));
        if let (false, Some(rec)) = (control, record) {
            let dig = dig
                .map(|d| d.into_bound(py))
                .unwrap_or_else(|| py.None().into_bound(py));
            let t = PyTuple::new(
                py,
                [
                    ins.node.bind(py).as_any(),
                    &dig,
                    PyString::new(py, "error").as_any(),
                    err.bind(py).as_any(),
                    &py.None().into_bound(py),
                ],
            )?;
            self.record_origin(py, job, &ins, rec.bind(py).len())?;
            self.record_invocation(py, job, x, rec.bind(py).len())?;
            rec.bind(py).append(t)?;
            let limit = self.h.knob(py, K::RecordLimit)?;
            if !limit.is_none() && rec.bind(py).len() as f64 > crate::lanes::lane::as_f64(&limit)? {
                let message = PyString::new(py, &format!("more than {} calls", limit.str()?));
                let mut job = job.borrow_mut();
                job.record = None;
                job.stats.extra.shift_remove("record_origins");
                job.stats.extra.shift_remove("record_invocations");
                job.stats
                    .extra
                    .insert("record_skipped".into(), message.into_any().unbind());
            }
        }
        let origin = ins.py.bind(py).call_method0(intern!(py, "origin_dict"))?;
        let item = x.borrow().item.as_ref().map(|i| i.clone_ref(py));
        let policy = match item {
            Some(item) => item
                .bind(py)
                .getattr(intern!(py, "ext"))?
                .call_method1(intern!(py, "get"), ("policy_failure",))?,
            None => py.None().into_bound(py),
        };
        if !policy.is_none() {
            let failure = policy.cast::<PyDict>()?.copy()?;
            failure.set_item("node", ins.node.bind(py))?;
            failure.set_item("addr", ins.addr)?;
            failure.set_item("key", key.to_py(py))?;
            failure.set_item("origin", &origin)?;
            let index = job.borrow().record.as_ref().map(|r| r.bind(py).len() - 1);
            failure.set_item("record_index", index)?;
            let failures = job
                .borrow_mut()
                .stats
                .extra
                .entry("policy_failures".into())
                .or_insert_with(|| PyList::empty(py).into_any().unbind())
                .clone_ref(py);
            failures.bind(py).cast::<PyList>()?.append(failure)?;
        }
        job.borrow_mut()
            .stats
            .extra
            .insert("fault_origin".into(), origin.clone().unbind());
        let (jid, jkey, entry) = {
            let j = job.borrow();
            (j.id, j.key.clone_ref(py), j.entry.clone())
        };
        let info = PyDict::new(py);
        info.set_item("job", jid)?;
        info.set_item("key", jkey.bind(py))?;
        info.set_item("origin", &origin)?;
        if !policy.is_none() {
            info.set_item("policy_failure", policy.cast::<PyDict>()?.copy()?)?;
        }
        info.update(err.bind(py).as_mapping())?;
        let where_ = PyDict::new(py);
        where_.set_item("entry", entry.py_name.bind(py))?;
        let kw = PyDict::new(py);
        kw.set_item("where", where_)?;
        self.h.plugins.bind(py).call_method(
            intern!(py, "notify"),
            ("on_fault", info),
            Some(&kw),
        )?;
        if self.wants(py, "fault.taken", Some(jid))? {
            let f = PyDict::new(py);
            f.set_item("job", jid)?;
            f.set_item("addr", ins.addr)?;
            f.set_item("key", key.to_py(py))?;
            f.set_item("squashed", job.borrow().stats.squashed)?;
            f.set_item("origin", &origin)?;
            if !policy.is_none() {
                f.set_item("policy_failure", policy.cast::<PyDict>()?.copy()?)?;
            }
            for (k, v) in err.bind(py).iter() {
                f.set_item(format!("error_{}", k.str()?), v)?;
            }
            self.emit(py, "fault.taken", &f)?;
        }
        let snap = {
            let b = x.borrow();
            let s = b.snapshot.as_ref().expect("a fault's snapshot");
            Snap {
                names: s.names.clone(),
                frames: s.frames.clone(),
                prefix: s.prefix.clone(),
                counter: s.counter,
            }
        };
        let catchable = x.borrow().fault.as_ref().expect("a fault").catchable;
        x.borrow_mut().release();

        // Inside a `@try`: continue at the handler with the faulting statement's view
        if let (true, Some(tid)) = (catchable, ins.try_id) {
            let info = self.code.borrow().tries.get(&tid).cloned().expect("a try");

            // Younger continuations were squashed: the handler strand carries on past each join
            let mut kept = Vec::with_capacity(snap.frames.len());
            for f in snap.frames.iter() {
                if f.branch.is_some_and(|b| info.body.contains(&b)) {
                    continue;
                }
                if f.join.is_some() {
                    let mut g = f.copy();
                    g.join = None;
                    if f.kind == FrameKind::Iter {
                        g.reconv = self
                            .code
                            .borrow()
                            .instr(f.each.expect("an each"))
                            .and_then(|e| e.reconv);
                    }
                    kept.push(Rc::new(g));
                } else {
                    kept.push(f.clone());
                }
            }
            let collectors = std::mem::take(&mut job.borrow_mut().collectors);
            for collector in collectors {
                if kept.iter().any(|f| {
                    f.collector
                        .as_ref()
                        .is_some_and(|c| Rc::ptr_eq(c, &collector))
                }) {
                    job.borrow_mut().collectors.push(collector.clone());
                    self.collect_finalize(py, job, &collector)?;
                } else {
                    collector.borrow_mut().abort();
                }
            }
            let mut nm = NameMap::of(&snap.names, None);
            if let Some(name) = info.error_name {
                let copy = err.bind(py).copy()?;
                nm.set(name, Slot::constant(copy.into_any().unbind()));
            }
            let st = Strand::new(
                job,
                jid,
                info.handler,
                snap.prefix.clone(),
                snap.counter,
                Rc::new(kept),
                Some(nm),
                None,
            );
            job.borrow_mut().add_strand(&st);
            self.mark_dirty(jid);
            return Ok(());
        }

        // Outside any `@try`: the job fails with the outputs written so far
        let outputs = PyDict::new(py);
        let missing = PyList::empty(py);
        for (name_py, name) in entry.outputs.iter().flatten() {
            let s = snap.names.get(*name);
            let v = s.map(|s| effective(&s, &key));
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
        job.borrow_mut().failure = Some(err);
        self.end_job(py, job, outputs, missing)
    }
}
