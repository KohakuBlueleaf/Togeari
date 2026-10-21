//! Fetch and rename, oldest key first. A pass without the GIL (``py`` None) stops, resumably,
//! before the first step that needs Python; that step runs once the GIL is back.

use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::rc::Rc;

use pyo3::prelude::*;

use crate::program::core::{Instr, Op, Operand};
use crate::runtime::core::Core;
use crate::runtime::events::{gil, Exc, Stop};
use crate::runtime::host::K;
use crate::runtime::state::frame::cut;
use crate::runtime::state::job::HeapItem;
use crate::runtime::state::{
    deref, effective, Inflight, JobRef, Key, Slot, SlotRef, SlotState, Snap, StrandRef,
    StrandState, Version,
};

// Instructions one front-end pass may run for a job before the job yields to others
const FETCH_BUDGET: i64 = 20_000;

/// The knobs fetch reads, once per pass (numbers as Python compares a count with them).
pub struct Caps {
    pub window: f64,
    pub retire_buffer: f64,
    pub step_budget: Option<f64>,
    pub step_text: String,
    pub run_ahead: bool,
}

type StrandHeap = BinaryHeap<Reverse<(Key, u64, HeapItem<StrandRef>)>>;

/// One job's front-end pass, resumable after a step that needed the GIL.
pub struct Pass {
    heap: StrandHeap,
    capped: StrandHeap,
    budget: i64,
    // The strand whose run stopped before a step that needs Python, and its steps so far
    cur: Option<(StrandRef, i64)>,
}

/// How a pass or a strand's run ended.
pub enum Flow {
    Done(i64),
    NeedGil(i64),
}

fn push(heap: &mut StrandHeap, st: StrandRef) {
    let (k, id) = {
        let s = st.borrow();
        (s.next_key(), s.id)
    };
    heap.push(Reverse((k, id, HeapItem(st))));
}

fn top_strand(heap: &StrandHeap) -> Option<StrandRef> {
    heap.peek().map(|e| e.0 .2 .0.clone())
}

/// ``v`` is True (Some(true)), False (Some(false)) or anything else (None).
fn as_bool(v: &Py<PyAny>) -> Option<bool> {
    let p = v.as_ptr();
    unsafe {
        if p == pyo3::ffi::Py_True() {
            Some(true)
        } else if p == pyo3::ffi::Py_False() {
            Some(false)
        } else {
            None
        }
    }
}

impl Core {
    pub fn caps(&self, py: Python<'_>) -> PyResult<Caps> {
        let num = |k: K| -> PyResult<f64> { crate::lanes::lane::as_f64(&self.h.knob(py, k)?) };
        Ok(Caps {
            window: num(K::Window)?,
            retire_buffer: num(K::RetireBuffer)?,
            step_budget: self.h.knob_f64(py, K::StepBudget)?,
            step_text: self.h.knob(py, K::StepBudget)?.str()?.to_string(),
            run_ahead: self.h.knob(py, K::RunAhead)?.is_truthy()?,
        })
    }

    // ------------------------------------------------------------ driving
    pub fn pass(job: &JobRef) -> Pass {
        let mut heap = StrandHeap::new();
        for s in job.borrow_mut().take_runnable() {
            push(&mut heap, s);
        }
        Pass { heap, capped: StrandHeap::new(), budget: FETCH_BUDGET, cur: None }
    }

    /// Run (or continue) a pass; ``NeedGil`` only without the GIL.
    pub fn run_pass(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        pass: &mut Pass,
        caps: &Caps,
    ) -> PyResult<Flow> {
        loop {
            let (st, n0, resumed) = match pass.cur.take() {
                Some((st, n)) => (st, n, true),
                None => {
                    if pass.budget <= 0 || job.borrow().ended {
                        break;
                    }
                    let Some(Reverse((_, _, HeapItem(st)))) = pass.heap.pop() else {
                        break;
                    };
                    if st.borrow().state != StrandState::Runnable {
                        continue;
                    }
                    (st, 0, false)
                }
            };
            match self.run(py, job, &st, pass.budget, n0, resumed, caps)? {
                Flow::Done(n) => pass.budget -= n,
                Flow::NeedGil(n) => {
                    pass.cur = Some((st, n));
                    return Ok(Flow::NeedGil(0));
                }
            }

            // Stopped at a cap: wait there; stopped for the budget or a yield: run again
            if st.borrow().state == StrandState::Runnable {
                if self.blocked_by_caps(job, &st, caps) {
                    push(&mut pass.capped, st);
                } else {
                    push(&mut pass.heap, st);
                }
            }

            // Strands made runnable meanwhile, and a capped strand that is now the oldest
            let more = job.borrow_mut().take_runnable();
            for s in more {
                push(&mut pass.heap, s);
            }
            while top_strand(&pass.capped)
                .is_some_and(|s| s.borrow().state != StrandState::Runnable)
            {
                pass.capped.pop();
            }
            if let Some(s) = top_strand(&pass.capped) {
                if !self.blocked_by_caps(job, &s, caps) {
                    let e = pass.capped.pop().expect("a capped strand");
                    pass.heap.push(e);
                }
            }
        }

        // Whatever did not run stays runnable; a budget stop runs again next cycle
        let again = !pass.heap.is_empty();
        let heap = std::mem::take(&mut pass.heap);
        let capped = std::mem::take(&mut pass.capped);
        for e in heap.into_vec().into_iter().chain(capped.into_vec()) {
            let s = e.0 .2 .0;
            if s.borrow().state == StrandState::Runnable {
                job.borrow_mut().mark_runnable(&s);
            }
        }
        let (ended, jid) = {
            let j = job.borrow();
            (j.ended, j.id)
        };
        if again && !ended {
            self.mark_dirty(jid);
        }
        Ok(Flow::Done(0))
    }

    pub fn blocked_by_caps(&self, job: &JobRef, st: &StrandRef, caps: &Caps) -> bool {
        {
            let j = job.borrow();
            if (j.unfinished as f64) < caps.window
                && ((j.window.len() as i64 - j.unfinished) as f64) < caps.retire_buffer
            {
                return false;
            }
        }

        // Progress rule: the strand holding the oldest unfetched statement fetches
        let key = st.borrow().next_key();
        if job.borrow_mut().oldest_live_key().as_ref() != Some(&key) {
            return true;
        }

        // A spawner waits while older unfinished work will free the window
        if st.borrow().spawn.is_some() {
            return job
                .borrow_mut()
                .oldest_open_key()
                .is_some_and(|older| older < key);
        }
        false
    }

    /// Fetch from one strand until it stops; ``n0`` steps were run before a resume, which skips
    /// the cap check and frame pops its stop had already made.
    #[allow(clippy::too_many_arguments)]
    fn run(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        st: &StrandRef,
        budget: i64,
        n0: i64,
        resumed: bool,
        caps: &Caps,
    ) -> PyResult<Flow> {
        if st.borrow().spawn.is_some() {
            return Ok(Flow::Done(if self.blocked_by_caps(job, st, caps) {
                0
            } else {
                self.spawn_next(py, job, st)?
            }));
        }
        let mut n = n0;
        let mut resume = resumed;
        loop {
            if !resume {
                if st.borrow().state != StrandState::Runnable || n >= budget {
                    break;
                }
                if self.blocked_by_caps(job, st, caps) {
                    break;
                }
                if !self.pop_frames(py, job, st)? {
                    n += 1;
                    break;
                }
            }
            resume = false;

            // One instruction; KIR runtime errors become uncatchable faults
            let pc = st.borrow().pc;
            let ins = self
                .code
                .borrow()
                .instr(pc)
                .expect("an instruction at the strand's pc");
            if py.is_none() {
                if !self.free_ok(job, st, &ins, caps) {
                    self.count_region(|r| r.gil_steps += 1);
                    return Ok(Flow::NeedGil(n));
                }
                self.count_region(|r| r.free_steps += 1);
            }
            n += 1;
            let steps = {
                let mut j = job.borrow_mut();
                j.steps += 1;
                j.steps
            };
            let step = match caps.step_budget {
                Some(limit) if steps as f64 > limit => {
                    Err(Stop::Fault(format!("more than {} steps", caps.step_text)))
                }
                _ => self.op(py, job, st, &ins, caps),
            };
            match step {
                Ok(()) => {}
                Err(Stop::Fault(msg)) => {
                    self.control_fault(gil(py), job, st, &ins, Exc::Runtime(msg))?;
                    break;
                }
                Err(Stop::Err(e)) => return Err(e),
            }

            if st.borrow().state == StrandState::Runnable {
                self.early_resolve(py, st)?;
            }
        }
        Ok(Flow::Done(n))
    }

    /// Whether the next step of ``st`` runs without Python: no literal to copy, no fault to
    /// build, no item list to read or case to compare.
    fn free_ok(&self, job: &JobRef, st: &StrandRef, ins: &Instr, caps: &Caps) -> bool {
        if caps
            .step_budget
            .is_some_and(|limit| (job.borrow().steps + 1) as f64 > limit)
        {
            return false;
        }
        let s = st.borrow();
        let names = s.names.as_ref();
        let operand_ok = |op: &Operand| match op {
            Operand::Name(n) => names.is_some_and(|m| m.get(*n).is_some()),
            Operand::Lit { container, .. } => !container,
            Operand::Missing(_) => false,
        };
        match ins.op {
            Op::Call => {
                ins.args.iter().all(|(_, op)| operand_ok(op))
                    && ins.mutates_args.iter().all(Option::is_some)
            }
            Op::Assign => ins.operand.as_ref().is_some_and(operand_ok),
            Op::Jump | Op::End => true,
            Op::Branch => {
                let Some(op) = ins.operand.as_ref() else {
                    return false;
                };
                if !operand_ok(op) {
                    return false;
                }
                let slot = match op {
                    Operand::Name(n) => names.and_then(|m| m.get(*n)),
                    _ => None,
                };
                let Some(slot) = slot else {
                    // A scalar literal condition is decided now: True or False only
                    return matches!(op, Operand::Lit { value, .. } if as_bool(value).is_some());
                };
                let e = effective(&slot, &s.next_key());
                let b = e.borrow();
                match b.state {
                    SlotState::Ready => b.value.as_ref().is_some_and(|v| as_bool(v).is_some()),
                    _ => true,
                }
            }
            Op::Switch | Op::Each | Op::IterEnd => false,
        }
    }

    fn op(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        st: &StrandRef,
        ins: &Rc<Instr>,
        caps: &Caps,
    ) -> Result<(), Stop> {
        match ins.op {
            Op::Call => self.op_call(py, job, st, ins),
            Op::Assign => self.op_assign(py, st, ins),
            Op::Branch => self.op_branch(py, job, st, ins, caps),
            Op::Switch => self.op_switch(gil(py), job, st, ins, caps),
            Op::Jump => {
                let mut s = st.borrow_mut();
                s.consume();
                s.pc = ins.targets[0];
                Ok(())
            }
            Op::Each => self.op_each(gil(py), job, st, ins, caps),
            Op::IterEnd => self.op_iter_end(gil(py), st, ins),
            Op::End => self.op_end(py, job, st),
        }
    }

    /// Leave every region whose reconvergence point is the strand's pc; False if it ended.
    fn pop_frames(&self, py: Option<Python<'_>>, job: &JobRef, st: &StrandRef) -> PyResult<bool> {
        loop {
            let top = {
                let s = st.borrow();
                match s.frames.last() {
                    Some(f) if f.reconv == Some(s.pc) => f.clone(),
                    _ => return Ok(true),
                }
            };
            {
                let mut s = st.borrow_mut();
                let n = s.frames.len() - 1;
                s.frames = cut(&s.frames, n);
            }
            if let Some(join) = &top.join {
                self.resolve_join(py, join, st)?;
                self.end(py, job, st)?;
                return Ok(false);
            }
            if let Some((p, c)) = &top.saved {
                let mut s = st.borrow_mut();
                s.prefix = p.clone();
                s.counter = *c;
            }
        }
    }

    pub fn end(&self, py: Option<Python<'_>>, job: &JobRef, st: &StrandRef) -> PyResult<()> {
        job.borrow_mut().end_strand(st);
        let jid = job.borrow().id;
        let (sid, pc) = {
            let s = st.borrow();
            (s.id, s.pc)
        };
        self.note_strand(py, "strand.end", jid, sid, pc, None)
    }

    // ------------------------------------------------------------ operands
    pub fn fresh(&self, py: Python<'_>, value: &Py<PyAny>, container: bool) -> PyResult<Py<PyAny>> {
        if container {
            Ok(self.h.deepcopy.bind(py).call1((value.bind(py),))?.unbind())
        } else {
            Ok(value.clone_ref(py))
        }
    }

    pub fn name_repr(&self, py: Python<'_>, id: u32) -> PyResult<String> {
        let code = self.code.borrow();
        Ok(code.name_py(py, id).repr()?.to_string())
    }

    pub fn slot_of(
        &self,
        py: Option<Python<'_>>,
        st: &StrandRef,
        op: &Operand,
    ) -> Result<SlotRef, Stop> {
        match op {
            Operand::Name(name) => {
                let got = st.borrow().names.as_ref().and_then(|n| n.get(*name));
                match got {
                    Some(s) => Ok(s),
                    None => Err(Stop::Fault(format!(
                        "{} is not defined",
                        self.name_repr(gil(py), *name)?
                    ))),
                }
            }
            Operand::Lit { value, container: false } => Ok(Slot::shared(value)),
            Operand::Lit { value, container: true } => {
                Ok(Slot::constant(self.fresh(gil(py), value, true)?))
            }
            Operand::Missing(port) => {
                Err(Stop::Fault(format!("input {} missing", port.bind(gil(py)).repr()?)))
            }
        }
    }

    pub fn snapshot_of(st: &StrandRef) -> Snap {
        let s = st.borrow();
        Snap {
            names: s.names.as_ref().expect("a strand with names").snapshot(),
            frames: s.frames.clone(),
            prefix: s.prefix.clone(),
            counter: s.counter,
        }
    }

    // ------------------------------------------------------------ faults
    fn control_fault(
        &self,
        py: Python<'_>,
        job: &JobRef,
        st: &StrandRef,
        ins: &Rc<Instr>,
        exc: Exc,
    ) -> PyResult<()> {
        let key = st.borrow_mut().consume();
        let jid = job.borrow().id;
        let inf = Inflight::new(job, jid, ins.clone(), key);
        {
            let mut x = inf.borrow_mut();
            x.control = true;
            x.snapshot = Some(Core::snapshot_of(st));
        }
        self.end(Some(py), job, st)?;
        job.borrow_mut().add(&inf);
        self.fault(py, &inf, exc, false)
    }

    // ------------------------------------------------------------ simple instructions
    fn op_call(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        st: &StrandRef,
        ins: &Rc<Instr>,
    ) -> Result<(), Stop> {
        let key = st.borrow_mut().consume();
        let jid = job.borrow().id;
        let inf = Inflight::new(job, jid, ins.clone(), key.clone());

        // Inputs: the slot each name holds now, or literals
        let mut inputs = Vec::with_capacity(ins.args.len());
        for (_, op) in &ins.args {
            inputs.push(self.slot_of(py, st, op)?);
        }

        // `mutates` ports: a new version of the value, visible to younger readers
        let mut versions = None;
        if !ins.mutates.is_empty() {
            let mut vs = Vec::with_capacity(ins.mutates.len());
            for (k, at) in ins.mutates_args.iter().enumerate() {
                let Some(at) = at else {
                    let port = ins.mutates[k].clone_ref(gil(py));
                    return Err(Stop::Err(pyo3::exceptions::PyKeyError::new_err(port)));
                };
                let before = inputs[*at].clone();
                let version = Slot::pending();
                deref(&before)
                    .borrow_mut()
                    .muts
                    .push((key.clone(), version.clone()));
                vs.push(Version { k, before, version });
            }
            versions = Some(vs);
        }
        {
            let mut x = inf.borrow_mut();
            x.inputs = inputs;
            x.snapshot = Some(Core::snapshot_of(st));
            x.versions = versions;
        }

        // Outputs: fresh slots, renamed into the strand's names
        let mut outs = Vec::with_capacity(ins.outs.len());
        {
            let mut s = st.borrow_mut();
            for o in &ins.outs {
                match o {
                    None => outs.push(None),
                    Some(name) => {
                        let slot = Slot::pending();
                        outs.push(Some(slot.clone()));
                        s.names
                            .as_mut()
                            .expect("a strand with names")
                            .set(*name, slot);
                    }
                }
            }
            s.pc = ins.next.expect("a call's next address");
        }
        inf.borrow_mut().outs = outs;
        self.admit_inflight(py, job, &inf)?;
        Ok(())
    }

    fn op_assign(
        &self,
        py: Option<Python<'_>>,
        st: &StrandRef,
        ins: &Rc<Instr>,
    ) -> Result<(), Stop> {
        st.borrow_mut().consume();
        let slot = self.slot_of(py, st, ins.operand.as_ref().expect("an assigned operand"))?;
        let mut s = st.borrow_mut();
        s.names
            .as_mut()
            .expect("a strand with names")
            .set(ins.target.expect("a target"), slot);
        s.pc = ins.next.expect("an assignment's next address");
        Ok(())
    }

    fn op_end(&self, py: Option<Python<'_>>, job: &JobRef, st: &StrandRef) -> Result<(), Stop> {
        let (key, names) = {
            let mut s = st.borrow_mut();
            let k = s.consume();
            (k, s.names.as_ref().expect("a strand with names").snapshot())
        };
        {
            let mut j = job.borrow_mut();
            j.final_names = Some(names);
            j.final_key = Some(key);
        }
        self.end(py, job, st)?;
        Ok(())
    }

    pub fn branch_value(v: &Py<PyAny>) -> Option<bool> {
        as_bool(v)
    }
}
