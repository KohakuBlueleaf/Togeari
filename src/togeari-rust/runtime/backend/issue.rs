//! Issue: ready statements to inline runs, store hits, or lane work items (spec/03 §3.7).

use std::collections::HashMap;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};

use crate::lanes::lane::{ENQUEUE, QUEUED, QUEUED_NOW, UNITS_UP};
use crate::program::core::StoreMode;
use crate::runtime::builtins::Tag;
use crate::runtime::core::Core;
use crate::runtime::events::{Exc, Res};
use crate::runtime::host::{is_exception, K};
use crate::runtime::python::Owner;
use crate::runtime::state::{effective, InfRef, InfState, JobRef, Key, Obj, SlotState, Waiter};

// Inline timing: the first median check, then one every this many samples
const FIRST_CHECK: u64 = 32;
const CHECK_EVERY: u64 = 1024;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Gate {
    Go,
    Copy,
    Wait,
}

/// The issue policy of this issue phase: the object, and the builtin bodies of its ``order``
/// and ``priority`` (None: called back).
pub struct IssueNow {
    pub obj: Obj,
    pub order: Option<Tag>,
    pub priority: Option<Tag>,
}

impl IssueNow {
    /// The order a region may take (a builtin body); None when it calls back.
    pub fn native_order(&self) -> Option<Tag> {
        self.order.filter(|t| {
            matches!(
                t,
                Tag::FifoOrder
                    | Tag::OldestOrder
                    | Tag::DeepestOrder
                    | Tag::LaneAwareOrder
                    | Tag::HintOrder
            )
        })
    }
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

impl Core {
    // ------------------------------------------------------------ readiness
    pub fn admit_inflight(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        inf: &InfRef,
    ) -> PyResult<()> {
        {
            let mut j = job.borrow_mut();
            j.add(inf);
            j.unfinished += 1;
            j.stats.calls += 1;
        }
        self.stats.borrow_mut().calls += 1;
        self.note_stmt(py, "stmt.fetch", inf)?;
        self.check(py, inf)
    }

    /// Ready when every input (through phis and older `mutates` versions) is filled.
    pub fn check(&self, py: Option<Python<'_>>, inf: &InfRef) -> PyResult<()> {
        let (inputs, key) = {
            let x = inf.borrow();
            (x.inputs.clone(), x.key.clone())
        };
        for slot in inputs {
            let s = effective(&slot, &key);
            let state = s.borrow().state;
            match state {
                SlotState::Pending => {
                    s.borrow_mut().waiters.push(Waiter::Inf(inf.clone()));
                    return Ok(());
                }
                SlotState::Squashed => return Ok(()),
                SlotState::Ready => {}
            }
        }
        if inf.borrow().collector.is_some() {
            let mut x = inf.borrow_mut();
            x.state = InfState::Done;
            self.mark_dirty(x.job_id);
            return Ok(());
        }
        inf.borrow_mut().state = InfState::Ready;
        self.ready.borrow_mut().push(inf.clone());
        self.note_stmt(py, "stmt.ready", inf)
    }

    /// Go, copy or wait, by older unfinished work and older live strands of the job.
    fn gate(&self, py: Python<'_>, job: &JobRef, inf: &InfRef) -> PyResult<Gate> {
        let (ordered, mutates, key) = {
            let x = inf.borrow();
            (x.ins.ordered, !x.ins.mutates.is_empty(), x.key.clone())
        };
        let (open_key, live_key) = {
            let mut j = job.borrow_mut();
            (j.oldest_open_key(), j.oldest_live_key())
        };
        // DONE semantic emits are not visible effects until their durable append
        // is accepted at retirement. Keep younger external effects behind them.
        let uncommitted_emit = job.borrow().window.live().iter().any(|prior| {
            let prior = prior.borrow();
            prior.key < key
                && prior.ins.semantic_emit
                && !matches!(prior.state, InfState::Retired | InfState::Squashed)
        });
        let speculative = open_key.is_some_and(|k| k < key)
            || live_key.is_some_and(|k| k < key)
            || uncommitted_emit;
        if ordered && speculative {
            return Ok(Gate::Wait);
        }
        if mutates {
            let mode = self.h.knob(py, K::Mutates)?;
            if mode.eq("copy-always")? {
                return Ok(Gate::Copy);
            }
            if speculative {
                return Ok(if mode.eq("wait")? {
                    Gate::Wait
                } else {
                    Gate::Copy
                });
            }
        }
        Ok(Gate::Go)
    }

    // ------------------------------------------------------------ the policy
    /// The issue knob's policy now.
    pub fn issue_now(&self, py: Python<'_>) -> PyResult<IssueNow> {
        let p = self.h.knob(py, K::Issue)?;
        let b = &self.h.builtins;
        Ok(IssueNow {
            order: b.method(py, &p, "order")?,
            priority: b.method(py, &p, "priority")?,
            obj: p.unbind(),
        })
    }

    /// A `ReadyView` of a statement, for a policy written in Python.
    fn ready_view<'py>(&self, py: Python<'py>, inf: &InfRef) -> PyResult<Bound<'py, PyAny>> {
        let (job_id, jkey, entry, instr, key) = {
            let x = inf.borrow();
            let job = x.job.upgrade().expect("a resident job");
            let j = job.borrow();
            (
                x.job_id,
                j.key.clone_ref(py),
                j.entry.py.clone_ref(py),
                x.ins.py.clone_ref(py),
                x.key.clone(),
            )
        };
        let r = self
            .h
            .ready_inf
            .bind(py)
            .call1((job_id, jkey, entry, instr, key.to_py(py)))?;
        self.h.ready_view.bind(py).call1((r,))
    }

    /// The fill of a lane the core adopted, read natively: queued items per unit up.
    fn native_fill(&self, lane: &Obj) -> Option<f64> {
        let lanes = self.lanes.borrow();
        let (_, n) = lanes.get(&(lane.as_ptr() as usize))?;
        let n = n.as_ref()?;
        let c = n.get().c();
        if c.overridden.get() & (QUEUED | QUEUED_NOW | UNITS_UP) != 0 {
            return None;
        }
        let mut st = c.st.borrow_mut();
        let up = st.units_up().len().max(1);
        Some(st.queued() as f64 / up as f64)
    }

    /// The order a builtin policy gives ``batch``, without Python; None when it needs Python (a
    /// lane run in Python, a hint priority Rust cannot hold).
    pub fn order_native(&self, plan: Tag, batch: &[InfRef]) -> Option<Vec<InfRef>> {
        let keyed = |f: &dyn Fn(&InfRef) -> Option<(f64, Key)>| -> Option<Vec<InfRef>> {
            let mut v = Vec::with_capacity(batch.len());
            for inf in batch {
                let (a, k) = f(inf)?;
                v.push((a, k, inf.clone()));
            }
            v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap().then_with(|| a.1.cmp(&b.1)));
            Some(v.into_iter().map(|x| x.2).collect())
        };
        match plan {
            Tag::FifoOrder => Some(batch.to_vec()),
            Tag::OldestOrder => keyed(&|i| {
                let x = i.borrow();
                Some((x.job_id as f64, x.key.clone()))
            }),
            Tag::DeepestOrder => {
                let mut v: Vec<(Key, InfRef)> = batch
                    .iter()
                    .map(|i| (i.borrow().key.clone(), i.clone()))
                    .collect();
                v.sort_by(|a, b| b.0.cmp(&a.0));
                Some(v.into_iter().map(|x| x.1).collect())
            }
            // A lane's fill does not change while the batch is ordered: read per statement
            Tag::LaneAwareOrder => keyed(&|i| {
                let x = i.borrow();
                let f = match &x.ins.binding {
                    Some(b) if !b.inline => self.native_fill(b.lane.as_ref()?)?,
                    _ => -1.0,
                };
                Some((f, x.key.clone()))
            }),
            // Ranks within 2**53 compare exactly as floats; others go to Python
            Tag::HintOrder => keyed(&|i| {
                let x = i.borrow();
                let rank = x.ins.hint_rank.filter(|r| r.unsigned_abs() < (1 << 53))?;
                Some((-(rank as f64), x.key.clone()))
            }),
            _ => None,
        }
    }

    /// The fills of the lanes of ``batch`` (Python lanes asked), then the LaneAware order.
    fn lane_aware(&self, py: Python<'_>, batch: &[InfRef]) -> PyResult<Vec<InfRef>> {
        let mut fills: Vec<(Obj, f64)> = Vec::new();
        let mut v = Vec::with_capacity(batch.len());
        for inf in batch {
            let lane = {
                let x = inf.borrow();
                match &x.ins.binding {
                    Some(b) if !b.inline => b.lane.as_ref().map(|l| l.clone_ref(py)),
                    _ => None,
                }
            };
            let f = match lane {
                None => -1.0,
                Some(l) => match fills.iter().find(|(x, _)| x.is(&l)) {
                    Some((_, f)) => *f,
                    None => {
                        let lb = l.bind(py);
                        let f = match self.native_for(py, lb, QUEUED | QUEUED_NOW | UNITS_UP)? {
                            Some(n) => {
                                let mut st = n.get().c().st.borrow_mut();
                                st.queued() as f64 / st.units_up().len().max(1) as f64
                            }
                            None => {
                                let q = lb.call_method0(intern!(py, "queued"))?;
                                let up = lb.call_method0(intern!(py, "units_up"))?.len()?;
                                crate::lanes::lane::as_f64(&q)? / up.max(1) as f64
                            }
                        };
                        fills.push((l, f));
                        f
                    }
                },
            };
            let k = inf.borrow().key.clone();
            v.push((f, k, inf.clone()));
        }
        v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap().then_with(|| a.1.cmp(&b.1)));
        Ok(v.into_iter().map(|x| x.2).collect())
    }

    /// The policy's order of ``batch``, each statement once, then the ones it left out in
    /// arrival order; a raising policy gives arrival order.
    fn issue_order(
        &self,
        py: Python<'_>,
        now: &IssueNow,
        batch: Vec<InfRef>,
    ) -> PyResult<Vec<InfRef>> {
        if let Some(plan) = now.native_order() {
            if let Some(v) = self.order_native(plan, &batch) {
                return Ok(v);
            }
            if plan == Tag::LaneAwareOrder {
                return self.lane_aware(py, &batch);
            }
        }

        // The policy's own ``order`` over views of this batch, each view once
        let views = PyList::empty(py);
        let mut at: HashMap<usize, usize> = HashMap::with_capacity(batch.len());
        for (i, inf) in batch.iter().enumerate() {
            let v = self.ready_view(py, inf)?;
            at.insert(v.as_ptr() as usize, i);
            views.append(v)?;
        }
        let chosen = match now
            .obj
            .bind(py)
            .call_method1(intern!(py, "order"), (views.clone(),))
            .and_then(|x| py.get_type::<PyList>().call1((x,)))
        {
            Ok(c) => c.cast_into::<PyList>()?,
            Err(e) if is_exception(py, &e) => {
                self.policy_failed(py, "issue order", &e);
                views.clone()
            }
            Err(e) => return Err(e),
        };
        let mut seen = vec![false; batch.len()];
        let mut out = Vec::with_capacity(batch.len());
        for view in chosen.iter() {
            if let Some(&i) = at.get(&(view.as_ptr() as usize)) {
                if !seen[i] {
                    seen[i] = true;
                    out.push(batch[i].clone());
                }
            }
        }
        for (i, inf) in batch.into_iter().enumerate() {
            if !seen[i] {
                out.push(inf);
            }
        }
        Ok(out)
    }

    /// The policy's lane priority for a statement; 0 when the policy raises.
    fn issue_priority(&self, py: Python<'_>, now: &IssueNow, inf: &InfRef) -> PyResult<Obj> {
        let int = |v: i64| -> PyResult<Obj> { Ok(v.into_pyobject(py)?.into_any().unbind()) };
        match now.priority {
            Some(Tag::FifoPriority | Tag::LaneAwarePriority) => int(0),
            Some(Tag::OldestPriority) => int(-(inf.borrow().job_id as i64)),
            Some(Tag::DeepestPriority) => int(inf.borrow().key.0.len() as i64),
            Some(Tag::HintPriority) => {
                let h = inf
                    .borrow()
                    .ins
                    .hint_priority
                    .as_ref()
                    .map(|v| v.clone_ref(py));
                let r = match h {
                    None => int(0),
                    Some(v) => Ok(py.get_type::<pyo3::types::PyInt>().call1((v,))?.unbind()),
                };
                match r {
                    Ok(p) => Ok(p),
                    Err(e) if is_exception(py, &e) => {
                        self.policy_failed(py, "issue priority", &e);
                        int(0)
                    }
                    Err(e) => Err(e),
                }
            }
            _ => {
                let view = self.ready_view(py, inf)?;
                match now
                    .obj
                    .bind(py)
                    .call_method1(intern!(py, "priority"), (view,))
                {
                    Ok(v) => Ok(v.unbind()),
                    Err(e) if is_exception(py, &e) => {
                        self.policy_failed(py, "issue priority", &e);
                        int(0)
                    }
                    Err(e) => Err(e),
                }
            }
        }
    }

    /// A user policy raised: counted in ``machine_errors`` and ``policy_errors``, logged at the
    /// 1st, 2nd, 4th, 8th ... error.
    pub fn policy_failed(&self, py: Python<'_>, what: &str, e: &PyErr) {
        self.stats.borrow_mut().machine_errors += 1;
        let n = self.count_extra("policy_errors");
        if n & (n - 1) == 0 {
            let msg = format!("{what} policy failed ({n} so far); fallback used");
            self.h.log(py, "error", &msg, Some(e));
        }
    }

    // ------------------------------------------------------------ issue
    /// Issue the ready statements in the order the issue policy gives them (``ordered``: the
    /// batch a region already ordered).
    pub fn issue(
        &self,
        py: Python<'_>,
        now: &IssueNow,
        ordered: Option<Vec<InfRef>>,
    ) -> PyResult<()> {
        let order = match ordered {
            Some(v) => v,
            None => {
                let batch = std::mem::take(&mut *self.ready.borrow_mut());
                if batch.is_empty() {
                    return Ok(());
                }
                self.issue_order(py, now, batch)?
            }
        };
        let mut now =
            IssueNow { obj: now.obj.clone_ref(py), order: now.order, priority: now.priority };
        for inf in order {
            if inf.borrow().state != InfState::Ready {
                continue;
            }
            let Some(job) = inf.borrow().job.upgrade() else {
                continue;
            };
            if job.borrow().event_cancel.is_some() {
                job.borrow_mut().held.push(inf.clone());
                continue;
            }
            if self.checkpoint_hold(&job, &inf) {
                continue;
            }
            if job.borrow().paused {
                job.borrow_mut().held.push(inf.clone());
                continue;
            }

            // The policy as set now (a plugin may have changed it meanwhile)
            if !self.h.knob(py, K::Issue)?.is(now.obj.bind(py)) {
                now = self.issue_now(py)?;
            }
            match self.issue_one(py, &now, &job, &inf) {
                Ok(()) => {}
                Err(e) if is_exception(py, &e) => self.job_error(py, &job, &e)?,
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }

    fn issue_one(
        &self,
        py: Python<'_>,
        now: &IssueNow,
        job: &JobRef,
        inf: &InfRef,
    ) -> PyResult<()> {
        let ins = inf.borrow().ins.clone();

        // Effects and in-place changes wait for (or copy around) older work
        let mut g = Gate::Go;
        if ins.ordered || !ins.mutates.is_empty() {
            g = self.gate(py, job, inf)?;
            if g == Gate::Wait {
                let jid = job.borrow().id;
                self.blocked
                    .borrow_mut()
                    .entry(jid)
                    .or_default()
                    .push(inf.clone());
                return Ok(());
            }
        }

        // Input values as this statement sees them
        let values = PyDict::new(py);
        let (inputs, key) = {
            let x = inf.borrow();
            (x.inputs.clone(), x.key.clone())
        };
        for ((port, _), slot) in ins.args.iter().zip(inputs) {
            let s = effective(&slot, &key);
            let v = s
                .borrow()
                .value
                .as_ref()
                .map(|v| v.clone_ref(py))
                .unwrap_or_else(|| py.None());
            if self.h.is_undef(py, &v) {
                let msg = format!(
                    "{}: input {} is not defined",
                    ins.node.bind(py),
                    port.bind(py).repr()?
                );
                return self.fault(py, inf, Exc::Runtime(msg), false);
            }
            values.set_item(port.bind(py), v)?;
        }

        // Copies for `mutates` ports when older work may still fail
        if g == Gate::Copy {
            let ports: Vec<Py<PyString>> = {
                let x = inf.borrow();
                x.versions
                    .iter()
                    .flatten()
                    .map(|v| ins.mutates[v.k].clone_ref(py))
                    .collect()
            };
            for port in ports {
                let v = values.get_item(port.bind(py))?.expect("a mutated input");
                values.set_item(port.bind(py), self.h.copy_value.bind(py).call1((v,))?)?;
            }
            inf.borrow_mut().copy = true;
            job.borrow_mut().stats.mutates_copies += 1;
        }
        inf.borrow_mut().values = Some(values.clone().unbind());

        // The inputs digest: for the record and the fingerprint check, and to find a result kept
        // from a squashed run of this statement
        let need =
            job.borrow().record.is_some() || self.h.knob(py, K::FingerprintInputs)?.is_truthy()?;
        let kept = job
            .borrow()
            .memo
            .as_ref()
            .is_some_and(|m| m.contains(ins.addr));
        if need || kept {
            let (dig, weak) = self.h.dg.digest_with_weak(values.as_any())?;
            if need {
                inf.borrow_mut().dig = Some(PyString::new(py, &hex(&dig)).into_any().unbind());
                if !weak.is_empty() {
                    self.note_weak(py, job, &weak)?;
                }
            }
            if kept && weak.is_empty() && self.take_reused(py, job, inf, &dig)? {
                return Ok(());
            }
        }
        self.execute(py, now, job, inf)
    }

    /// Actual execution/submission only: cache resolution never allocates an attempt.
    pub(crate) fn begin_attempt(&self, py: Python<'_>, inf: &InfRef) -> PyResult<String> {
        let attempt: String = self.h.new_attempt_id.bind(py).call0()?.extract()?;
        let mut inf = inf.borrow_mut();
        inf.attempt_ids.push(attempt.clone());
        inf.resolution = "executed";
        Ok(attempt)
    }

    fn execute(&self, py: Python<'_>, now: &IssueNow, job: &JobRef, inf: &InfRef) -> PyResult<()> {
        let ins = inf.borrow().ins.clone();
        let b = ins.binding.as_ref().expect("a bound call");
        inf.borrow_mut().t_issue = self.perf();
        let values = inf
            .borrow()
            .values
            .as_ref()
            .expect("issued values")
            .clone_ref(py);

        if ins.semantic_emit {
            if let Err(e) = self.prepare_event(py, job, inf) {
                if is_exception(py, &e) {
                    return self.fault(
                        py,
                        inf,
                        Exc::Obj(e.value(py).clone().into_any().unbind()),
                        true,
                    );
                }
                return Err(e);
            }
        }

        // Inline: run on the dispatcher now
        if b.inline {
            let t0 = self.perf();
            let f = b.func.as_ref().expect("an inline function").bind(py);
            self.begin_attempt(py, inf)?;
            let res = match f.call((), Some(values.bind(py))) {
                Ok(v) => Res::Ok(v.unbind(), true),
                Err(e) if is_exception(py, &e) => {
                    Res::ItemError(e.value(py).clone().into_any().unbind())
                }
                Err(e) => return Err(e),
            };
            let dt = self.perf() - t0;
            self.note_inline(py, &ins.node, dt, b.warn_us)?;
            job.borrow_mut().issue(inf);
            return self.complete(py, inf, res, None, None);
        }

        // Store lookup
        if b.store != StoreMode::No && self.h.store.is_some() {
            let key = self.store_key(py, job, inf)?;
            inf.borrow_mut().store_key = key.as_ref().map(|k| k.clone_ref(py));
            if let Some(key) = key {
                let hit = self.store_get(py, &key)?;
                let eligible = self.result_store_allowed(py, job, &ins)?;
                if !eligible {
                    inf.borrow_mut().store_key = None;
                }
                if eligible && !hit.bind(py).is(self.h.miss.bind(py)) {
                    self.stats.borrow_mut().store_hits += 1;
                    {
                        let mut inf = inf.borrow_mut();
                        inf.hit = true;
                        inf.resolution = if b.store == StoreMode::Content {
                            "content_hit"
                        } else {
                            "positional_hit"
                        };
                    }
                    job.borrow_mut().issue(inf);
                    return self.complete(py, inf, Res::Ok(hit, true), None, None);
                }
                self.stats.borrow_mut().store_misses += 1;
            }
        }

        // The call budget starts before preparation and policy callbacks.
        let t = self.now(py)?;
        let priority = self.issue_priority(py, now, inf)?;
        let (jid, jkey, entry_name, job_execution_id) = {
            let j = job.borrow();
            (
                j.id,
                j.key.clone_ref(py),
                j.entry.py_name.clone_ref(py),
                j.job_execution_id.clone(),
            )
        };
        let invocation_id = inf.borrow().invocation_id.clone().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("call has no invocation identity")
        })?;
        let (inf_id, key) = {
            let x = inf.borrow();
            (x.id, x.key.to_py(py))
        };
        let shell = self
            .shell
            .borrow()
            .as_ref()
            .expect("a bound dispatcher")
            .clone_ref(py);
        let lane_name = b
            .lane_name
            .as_ref()
            .map(|l| l.clone_ref(py).into_any())
            .unwrap_or_else(|| py.None());
        let owner = Py::new(
            py,
            Owner::new(
                inf_id,
                jid,
                ins.py.clone_ref(py),
                key.into_any().unbind(),
                entry_name.into_any(),
                lane_name,
                shell,
                self.h.job_view.clone_ref(py),
                job_execution_id.clone(),
                invocation_id.clone(),
            ),
        )?;
        let kw = PyDict::new(py);
        kw.set_item(intern!(py, "protocol"), self.h.protocol.bind(py))?;
        kw.set_item(intern!(py, "priority"), priority)?;
        kw.set_item(intern!(py, "owner"), &owner)?;
        kw.set_item("job_execution_id", &job_execution_id)?;
        kw.set_item("invocation_id", &invocation_id)?;
        let item = self.h.work_item.bind(py).call(
            (ins.node.bind(py), values.bind(py), b.adapter.as_ref().map(|a| a.bind(py))),
            Some(&kw),
        )?;
        let ext = item.getattr(intern!(py, "ext"))?;
        if let Some(timeout) = b.timeout_s {
            item.setattr(intern!(py, "deadline"), t + timeout)?;
            ext.set_item("call_timeout_s", timeout)?;
            ext.set_item("policy_source", &b.policy_source)?;
        }
        ext.set_item(intern!(py, "job_key"), jkey)?;
        ext.set_item(intern!(py, "job"), jid)?;
        ext.set_item(intern!(py, "hint"), ins.hint.bind(py))?;
        if ins.ordered && !b.idempotent {
            ext.set_item(intern!(py, "ordered_once"), true)?;
        }
        {
            let mut x = inf.borrow_mut();
            x.item = Some(item.clone().unbind());
            x.owner = Some(owner);
        }
        self.owned
            .borrow_mut()
            .insert(inf_id, std::rc::Rc::downgrade(inf));
        job.borrow_mut().issue(inf);
        let lane = b.lane.as_ref().expect("a lane").bind(py);
        match self.native_for(py, lane, ENQUEUE)? {
            Some(n) => n.get().c().enqueue(py, &item, t)?,
            None => {
                lane.call_method1(intern!(py, "enqueue"), (&item, t))?;
            }
        }
        if self.wants(py, "stmt.issue", Some(jid))? {
            self.emit_stmt(py, "stmt.issue", inf, None)?;
        }
        if self.wants(py, "lane.enqueue", Some(jid))? {
            let f = PyDict::new(py);
            f.set_item("job", jid)?;
            f.set_item("lane", lane.getattr(intern!(py, "name"))?)?;
            f.set_item("node", ins.node.bind(py))?;
            self.emit(py, "lane.enqueue", &f)?;
        }
        Ok(())
    }

    // ------------------------------------------------------------ stores
    /// The store key of a statement; None when its inputs (content) or the job's inputs
    /// (positional) digest only by type (weak).
    fn store_key(&self, py: Python<'_>, job: &JobRef, inf: &InfRef) -> PyResult<Option<Obj>> {
        let ins = inf.borrow().ins.clone();
        let b = ins.binding.as_ref().expect("a bound call");
        let Some(execution_hash) = self.result_execution(py, job, &ins)? else {
            self.stats.borrow_mut().store_misses += 1;
            return Ok(None);
        };
        let t = match b.store {
            // By the inputs' content
            StoreMode::Content => {
                let values = inf.borrow().values.as_ref().expect("values").clone_ref(py);
                let (dig, weak) = self.h.dg.digest_with_weak(values.bind(py).as_any())?;
                if !weak.is_empty() {
                    self.stats.borrow_mut().store_weak += 1;
                    self.note_weak(py, job, &weak)?;
                    return Ok(None);
                }
                PyTuple::new(
                    py,
                    [
                        PyString::new(py, "togeari.result").as_any(),
                        &2_i64.into_pyobject(py)?.into_any(),
                        PyString::new(py, "content").as_any(),
                        execution_hash.bind(py),
                        ins.node.bind(py).as_any(),
                        PyBytes::new(py, &dig).as_any(),
                    ],
                )?
            }

            // Same-invocation recovery only; equal fresh submissions never share this key.
            StoreMode::Positional => {
                let invocation = inf.borrow().invocation_id.clone().ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err(
                        "positional result has no invocation identity",
                    )
                })?;
                PyTuple::new(
                    py,
                    [
                        PyString::new(py, "togeari.result").as_any(),
                        &3_i64.into_pyobject(py)?.into_any(),
                        PyString::new(py, "positional").as_any(),
                        execution_hash.bind(py),
                        PyString::new(py, &invocation).as_any(),
                    ],
                )?
            }
            StoreMode::No => return Ok(None),
        };
        let d = self.h.dg.digest(t.as_any())?;
        // Input digest hooks may change live configuration before a lookup is attempted.
        if !self.result_store_allowed(py, job, &ins)? {
            self.stats.borrow_mut().store_misses += 1;
            return Ok(None);
        }
        Ok(Some(PyBytes::new(py, &d).into_any().unbind()))
    }

    fn result_execution(
        &self,
        py: Python<'_>,
        job: &JobRef,
        ins: &crate::program::core::Instr,
    ) -> PyResult<Option<Obj>> {
        let region = self
            .h
            .code
            .bind(py)
            .call_method1(intern!(py, "region_of"), (ins.addr,))?;
        let stats = PyDict::new(py);
        {
            let j = job.borrow();
            for name in [
                "execution",
                "execution_epoch",
                "persistence",
                "_persistence_context",
            ] {
                if let Some(value) = j.stats.extra.get(name) {
                    stats.set_item(name, value.bind(py))?;
                }
            }
        }
        let hash = self.h.result_execution.bind(py).call1((
            region,
            self.h.code.bind(py),
            self.h.machine.bind(py),
            &stats,
        ))?;
        for name in ["persistence", "_persistence_context"] {
            if let Some(value) = stats.get_item(name)? {
                job.borrow_mut()
                    .stats
                    .extra
                    .insert(name.into(), value.unbind());
            }
        }
        Ok(if hash.is_none() {
            None
        } else {
            Some(hash.unbind())
        })
    }

    pub fn result_store_allowed(
        &self,
        py: Python<'_>,
        job: &JobRef,
        ins: &crate::program::core::Instr,
    ) -> PyResult<bool> {
        Ok(self.result_execution(py, job, ins)?.is_some())
    }

    pub fn store_get(&self, py: Python<'_>, key: &Obj) -> PyResult<Obj> {
        let store = self.h.store.as_ref().expect("a store").bind(py);
        match store.call_method1(intern!(py, "get"), (key.bind(py),)) {
            Ok(v) => Ok(v.unbind()),
            Err(e) if is_exception(py, &e) => {
                self.store_error(py, "get", &e)?;
                Ok(self.h.miss.clone_ref(py))
            }
            Err(e) => Err(e),
        }
    }

    pub fn store_put(&self, py: Python<'_>, key: &Obj, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let store = self.h.store.as_ref().expect("a store").bind(py);
        match store.call_method1(intern!(py, "put"), (key.bind(py), value)) {
            Ok(_) => Ok(()),
            Err(e) if is_exception(py, &e) => self.store_error(py, "put", &e),
            Err(e) => Err(e),
        }
    }

    pub fn store_delete(&self, py: Python<'_>, key: &Obj) -> PyResult<()> {
        let store = self.h.store.as_ref().expect("a store").bind(py);
        let r = match store.getattr_opt(intern!(py, "delete"))? {
            Some(f) if !f.is_none() => f.call1((key.bind(py),)).map(|_| ()),
            _ => store
                .call_method1(intern!(py, "put"), (key.bind(py), py.None()))
                .map(|_| ()),
        };
        match r {
            Ok(()) => Ok(()),
            Err(e) if is_exception(py, &e) => self.store_error(py, "delete", &e),
            Err(e) => Err(e),
        }
    }

    pub fn store_error(&self, py: Python<'_>, op: &str, e: &PyErr) -> PyResult<()> {
        self.stats.borrow_mut().store_errors += 1;
        let text = e.value(py).str()?.to_string();
        self.h
            .log(py, "warning", &format!("store {op} failed: {text}"), Some(e));
        if self.wants(py, "store.error", None)? {
            let f = PyDict::new(py);
            let t = e.get_type(py).name()?.to_string();
            f.set_item("op", op)?;
            f.set_item("error", format!("{t}: {text}"))?;
            self.emit(py, "store.error", &f)?;
        }
        Ok(())
    }

    // ------------------------------------------------------------ reports
    fn note_inline(
        &self,
        py: Python<'_>,
        node: &Py<PyString>,
        dt: f64,
        warn_us: Option<f64>,
    ) -> PyResult<()> {
        // The node's entry by its name string (found by name once per string)
        let p = node.as_ptr() as usize;
        let at = self.inline_at.borrow().get(&p).copied();
        let i = match at {
            Some(i) => i,
            None => {
                let name = node.bind(py).to_string();
                let mut inline = self.inline.borrow_mut();
                let i = match inline.get_index_of(&name) {
                    Some(i) => i,
                    None => inline.insert_full(name, Default::default()).0,
                };
                self.inline_at.borrow_mut().insert(p, i);
                i
            }
        };
        let (warned, med) = {
            let mut inline = self.inline.borrow_mut();
            let stat = &mut inline[i];
            stat.add(dt);
            let due = stat.n == FIRST_CHECK || stat.n.is_multiple_of(CHECK_EVERY);
            (stat.warned, due.then(|| stat.median()))
        };
        let (Some(med), false) = (med, warned) else {
            return Ok(());
        };
        let limit = match warn_us {
            Some(w) => Some(w.into_pyobject(py)?.into_any()),
            None => Some(self.h.knob(py, K::InlineWarnUs)?).filter(|k| !k.is_none()),
        };
        let Some(limit) = limit else { return Ok(()) };
        let lim = crate::lanes::lane::as_f64(&limit)?;
        if med * 1e6 > lim {
            let name = {
                let mut inline = self.inline.borrow_mut();
                let (name, stat) = inline.get_index_mut(i).expect("an inline node");
                stat.warned = true;
                name.clone()
            };
            let msg =
                format!("inline node {name} median {:.1} us > {} us", med * 1e6, limit.str()?);
            self.h.log(py, "warning", &msg, None);
        }
        Ok(())
    }

    /// Values compared by type only (unpicklable): counted per job and per machine.
    pub fn note_weak(&self, py: Python<'_>, job: &JobRef, weak: &[String]) -> PyResult<()> {
        let mine = {
            let mut j = job.borrow_mut();
            let e = j
                .stats
                .extra
                .entry("weak_digests".to_string())
                .or_insert_with(|| PyDict::new(py).into_any().unbind());
            e.clone_ref(py)
        };
        let mine = mine.bind(py);
        for t in weak {
            let n: i64 = mine
                .get_item(t)
                .ok()
                .and_then(|v| v.extract().ok())
                .unwrap_or(0);
            mine.set_item(t, n + 1)?;
            let first = !self.stats.borrow().weak_digests.contains_key(t);
            if first {
                let msg = format!("values of type {t} digest by type only (weak comparison)");
                self.h.log(py, "warning", &msg, None);
            }
            *self
                .stats
                .borrow_mut()
                .weak_digests
                .entry(t.clone())
                .or_insert(0) += 1;
        }
        Ok(())
    }
}
