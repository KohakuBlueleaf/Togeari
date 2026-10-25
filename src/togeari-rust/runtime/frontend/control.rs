//! Branches without guessing: park, continuation strands, phis and joins (spec/03 §3.4).

use std::rc::Rc;

use indexmap::IndexMap;
use pyo3::prelude::*;

use crate::program::core::{Instr, NameId, NameSet, Table};
use crate::runtime::core::Core;
use crate::runtime::events::{gil, Stop};
use crate::runtime::frontend::fetch::Caps;
use crate::runtime::state::frame::{cut, pushed};
use crate::runtime::state::{
    effective, Frame, FrameKind, JobRef, Join, JoinRef, NameMap, Names, Resume, Slot, SlotRef,
    SlotState, Strand, StrandRef, StrandState, Val, Waiter,
};
use crate::telemetry::core::{self as tel, Put};

impl Core {
    // ------------------------------------------------------------ joins
    /// The names a join needs phis for, sorted: the region's writes, or every name with `ALL`.
    pub fn phi_names(&self, names: Option<&Names>, writes: &NameSet) -> Vec<NameId> {
        let code = self.code.borrow();
        if writes.all {
            let mut all: Vec<NameId> = names.map(|n| n.keys()).unwrap_or_default();
            all.extend_from_slice(writes.ids());
            code.names.sorted(all)
        } else {
            code.names.sorted(writes.ids().iter().copied())
        }
    }

    pub fn make_join(
        &self,
        names: Option<&Names>,
        writes: &NameSet,
        table: Option<Rc<Table>>,
    ) -> JoinRef {
        let phis: IndexMap<NameId, SlotRef> = self
            .phi_names(names, writes)
            .into_iter()
            .map(|n| (n, Slot::phi()))
            .collect();
        Join::new(phis, table, writes.all)
    }

    pub fn undefined(&self) -> SlotRef {
        Slot::shared(&self.h.undef_val)
    }

    pub fn resolve_join(
        &self,
        py: Option<Python<'_>>,
        join: &JoinRef,
        st: &StrandRef,
    ) -> PyResult<()> {
        let phis: Vec<(NameId, SlotRef)> = join
            .borrow()
            .phis
            .iter()
            .map(|(n, s)| (*n, s.clone()))
            .collect();
        for (n, phi) in phis {
            let open = {
                let p = phi.borrow();
                p.forward.is_none() && p.phi
            };
            if open {
                let target = st.borrow().names.as_ref().and_then(|m| m.get(n));
                let target = target.unwrap_or_else(|| self.undefined());
                self.resolve_phi(py, &phi, &target)?;
            }
        }
        join.borrow_mut().resolved = true;
        Ok(())
    }

    /// Resolve a phi as soon as no remaining path of the region can write its name.
    pub fn early_resolve(&self, py: Option<Python<'_>>, st: &StrandRef) -> PyResult<()> {
        let (f, pc) = {
            let s = st.borrow();
            match s.frames.iter().rev().find(|f| f.join.is_some()) {
                Some(f) => (f.clone(), s.pc),
                None => return Ok(()),
            }
        };
        let join = f.join.clone().expect("a join");
        let table = {
            let j = join.borrow();
            if j.resolved {
                return Ok(());
            }
            match &j.table {
                Some(t) => t.clone(),
                None => return Ok(()),
            }
        };
        let empty = NameSet::default();
        let w = if f.reconv != Some(pc) {
            table.get(&pc).unwrap_or(&empty)
        } else {
            &empty
        };
        if w.all {
            return Ok(());
        }

        let phis: Vec<(NameId, SlotRef)> = join
            .borrow()
            .phis
            .iter()
            .map(|(n, s)| (*n, s.clone()))
            .collect();
        let mut done = true;
        for (n, phi) in phis {
            if phi.borrow().forward.is_some() {
                continue;
            }
            if w.contains(n) {
                done = false;
                continue;
            }
            let target = st.borrow().names.as_ref().and_then(|m| m.get(n));
            let target = target.unwrap_or_else(|| self.undefined());
            self.resolve_phi(py, &phi, &target)?;
        }
        join.borrow_mut().resolved = done;
        Ok(())
    }

    // ------------------------------------------------------------ loops
    /// The index of the open frame the control instruction at ``addr`` opened, when the strand
    /// has come back to it (a loop); only unparked branch frames may lie above it.
    pub fn own_frame(st: &StrandRef, addr: i64) -> Option<usize> {
        let s = st.borrow();
        for i in (0..s.frames.len()).rev() {
            let f = &s.frames[i];
            if f.kind != FrameKind::Branch {
                return None;
            }
            if f.branch == Some(addr) {
                return Some(i);
            }
            if f.join.is_some() {
                return None;
            }
        }
        None
    }

    /// Back at the branch of frame ``i``: the frames above it close, and the strand keeps
    /// numbering inside frame ``i``'s arm after every key those frames used.
    fn reenter(st: &StrandRef, i: usize) {
        let mut s = st.borrow_mut();
        if i + 1 < s.frames.len() {
            let (p, c) = s.frames[i + 1].saved.clone().expect("a saved key");
            s.prefix = p;
            s.counter = c;
            s.frames = cut(&s.frames, i + 1);
        }
    }

    // ------------------------------------------------------------ deciding
    /// The ready value of the operand, or None after parking / stalling.
    pub fn control_value(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        st: &StrandRef,
        ins: &Rc<Instr>,
        caps: &Caps,
    ) -> Result<Option<Val>, Stop> {
        let slot = self.slot_of(py, st, ins.operand.as_ref().expect("a control operand"))?;
        let key = st.borrow().next_key();
        let s = effective(&slot, &key);
        let (state, value) = {
            let b = s.borrow();
            (b.state, b.value.clone())
        };
        if state == SlotState::Ready {
            let v = value.expect("a filled value");
            if v.as_ptr() == self.h.undef.as_ptr() {
                let name = ins
                    .operand_name
                    .as_ref()
                    .expect("an operand")
                    .bind(gil(py))
                    .to_string();
                return Err(Stop::Fault(format!("{name} is not defined")));
            }
            return Ok(Some(v));
        }

        // Pending: run ahead past a structured region, or wait here; a loop back at its own
        // branch waits inside the region it already holds
        let resume = st.borrow().resume;
        let can_park = resume != Resume::Parked
            && caps.run_ahead
            && ins.structured
            && !job.borrow().paused
            && job.borrow().cut.is_none()
            && Core::own_frame(st, ins.addr).is_none();
        if can_park {
            self.park(py, job, st, ins, &s)?;
        } else {
            {
                let mut b = st.borrow_mut();
                if resume == Resume::Parked {
                    b.state = StrandState::Parked;
                } else {
                    b.state = StrandState::Stalled;
                    b.resume = Resume::Stalled;
                }
            }
            s.borrow_mut().waiters.push(Waiter::Strand(st.clone()));
        }
        Ok(None)
    }

    fn park(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        st: &StrandRef,
        ins: &Rc<Instr>,
        waiting_on: &SlotRef,
    ) -> PyResult<()> {
        let reconv = ins
            .reconv
            .expect("a structured region's reconvergence point");
        let (key, names_now, prefix, counter, frames) = {
            let mut s = st.borrow_mut();
            let key = s.consume();
            let names = s.names.as_ref().expect("a strand with names").snapshot();
            (key, names, s.prefix.clone(), s.counter, s.frames.clone())
        };
        let jid = job.borrow().id;

        // A continuation strand at the reconvergence point reads phis for the region's writes
        let join = self.make_join(Some(&names_now), &ins.may_write, ins.region_writes.clone());
        let phis = join.borrow().phis.clone();
        let names = NameMap::of(&names_now, Some(phis));
        let c = Strand::new(
            job,
            jid,
            reconv,
            prefix.clone(),
            counter,
            frames.clone(),
            Some(names),
            None,
        );
        job.borrow_mut().add_strand(&c);

        // The parked strand keeps the region: its join frame and the arm's key prefix
        {
            let mut s = st.borrow_mut();
            s.frames = pushed(
                &frames,
                Frame::branch(Some(reconv), (prefix, counter), Some(join), ins.addr),
            );
            s.prefix = key.extend(1);
            s.counter = 0;
            s.state = StrandState::Parked;
            s.resume = Resume::Parked;
        }
        job.borrow_mut().stats.parks += 1;
        waiting_on
            .borrow_mut()
            .waiters
            .push(Waiter::Strand(st.clone()));
        let (sid, cid) = (st.borrow().id, c.borrow().id);
        if self.tel_on() {
            let mut p = Put::new(tel::PARK, jid, Some(ins.addr));
            p.aux = sid;
            self.tel(&p);
        }
        self.note_strand(py, "strand.park", jid, sid, ins.addr, Some(cid))
    }

    /// Take a decided branch: open its key frame (structured) and jump.
    fn enter_region(st: &StrandRef, ins: &Instr, target: i64) {
        let mut s = st.borrow_mut();
        if s.resume == Resume::Parked {
            s.resume = Resume::No;
            s.pc = target;
            return;
        }

        // A loop back at its own branch: the branch takes its key inside the frame it opened
        s.resume = Resume::No;
        drop(s);
        if let Some(i) = Core::own_frame(st, ins.addr) {
            Core::reenter(st, i);
            let mut s = st.borrow_mut();
            s.consume();
            s.pc = target;
            return;
        }

        // Not parked: the branch takes its key now and opens a frame if structured
        let mut s = st.borrow_mut();
        let key = s.consume();
        if ins.structured {
            let saved = (s.prefix.clone(), s.counter);
            s.frames = pushed(&s.frames, Frame::branch(ins.reconv, saved, None, ins.addr));
            s.prefix = key.extend(1);
            s.counter = 0;
        }
        s.pc = target;
    }

    // ------------------------------------------------------------ instructions
    pub fn op_branch(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        st: &StrandRef,
        ins: &Rc<Instr>,
        caps: &Caps,
    ) -> Result<(), Stop> {
        let Some(v) = self.control_value(py, job, st, ins, caps)? else {
            return Ok(());
        };
        match Core::branch_value(&v) {
            Some(cond) => {
                let target = if cond { ins.targets[0] } else { ins.targets[1] };
                Core::enter_region(st, ins, target);
                Ok(())
            }
            None => {
                let t = v.bind(gil(py)).get_type().name()?.to_string();
                Err(Stop::Fault(format!("branch condition is {t}, not bool")))
            }
        }
    }

    pub fn op_switch(
        &self,
        py: Python<'_>,
        job: &JobRef,
        st: &StrandRef,
        ins: &Rc<Instr>,
        caps: &Caps,
    ) -> Result<(), Stop> {
        let Some(v) = self.control_value(Some(py), job, st, ins, caps)? else {
            return Ok(());
        };
        let v = v.bind(py);
        let mut target = None;
        for (case, t) in &ins.cases {
            if case.bind(py).eq(v)? {
                target = Some(*t);
                break;
            }
        }
        match target.or(ins.default) {
            Some(t) => {
                Core::enter_region(st, ins, t);
                Ok(())
            }
            None => Err(Stop::Fault(format!("switch: no case matches {}", v.repr()?))),
        }
    }
}
