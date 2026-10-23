//! Conservative requested checkpoint cuts; artifact IO belongs to the Python manager.
use crate::runtime::core::Core;
use crate::runtime::host::is_exception;
use crate::runtime::state::frame::Frames;
use crate::runtime::state::job::CutRequest;
use crate::runtime::state::key::{prefix_to_py, Prefix};
use crate::runtime::state::{
    effective, FrameKind, InfRef, InfState, JobRef, Key, NameMap, Names, Resume, SlotState, Strand,
    StrandState,
};
use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::HashSet;
use std::rc::Rc;

struct Candidate {
    key: Key,
    pc: i64,
    prefix: Prefix,
    counter: i64,
    frames: Frames,
    names: Names,
}

impl Core {
    pub(crate) fn checkpoint_begin(
        &self,
        py: Python<'_>,
        id: u64,
        request: String,
        deadline: f64,
        step_budget: i64,
    ) -> PyResult<Py<PyDict>> {
        let reply = PyDict::new(py);
        let Some(job) = self.job(id) else {
            reply.set_item("status", "not_found")?;
            return Ok(reply.unbind());
        };
        let status = {
            let job = job.borrow();
            if job.ended {
                Some("terminal")
            } else if job.cut.is_some() {
                Some("busy")
            } else if job.inputs_dig.is_none() {
                Some("weak_inputs")
            } else {
                None
            }
        };
        if let Some(status) = status {
            reply.set_item("status", status)?;
            return Ok(reply.unbind());
        }
        if !deadline.is_finite() || step_budget < 0 {
            reply.set_item("status", "invalid")?;
            return Ok(reply.unbind());
        }
        let frontier = {
            let job = job.borrow();
            let mut keys: Vec<Key> = job
                .window
                .live()
                .iter()
                .filter_map(|inf| {
                    let inf = inf.borrow();
                    matches!(inf.state, InfState::Issued | InfState::Done).then(|| inf.key.clone())
                })
                .collect();
            keys.extend(job.checkpoint_watermark.clone());
            for collector in &job.collectors {
                let collector = collector.borrow();
                if collector.active {
                    keys.push(Key::of(&collector.key.0, collector.count as i64));
                }
            }
            keys.into_iter().max()
        };
        let start_steps = job.borrow().steps;
        job.borrow_mut().cut = Some(CutRequest {
            id: request,
            deadline,
            step_budget,
            start_steps,
            extra_steps: 0,
            frontier,
            held: vec![],
        });
        self.checkpoint_count.set(self.checkpoint_count.get() + 1);
        reply.set_item("status", "pending")?;
        reply.set_item("request_id", job.borrow().cut.as_ref().map(|cut| cut.id.as_str()))?;
        self.mark_dirty(id);
        self.checkpoint_progress(py)?;
        Ok(reply.unbind())
    }

    /// Hard gate before generic oldest-strand/window progress exemptions.
    pub(crate) fn checkpoint_blocks(&self, job: &JobRef, key: &Key) -> bool {
        let job = job.borrow();
        job.cut.as_ref().is_some_and(|cut| {
            job.steps
                .saturating_sub(cut.start_steps)
                .saturating_add(cut.extra_steps)
                >= cut.step_budget
                || cut.frontier.as_ref().is_none_or(|frontier| key > frontier)
        })
    }
    pub(crate) fn checkpoint_hold(&self, job: &JobRef, inf: &InfRef) -> bool {
        let key = inf.borrow().key.clone();
        if !self.checkpoint_blocks(job, &key) {
            return false;
        }
        if let Some(cut) = &mut job.borrow_mut().cut {
            cut.held.push(inf.clone());
        }
        true
    }
    pub(crate) fn checkpoint_extend_collectors(&self, job: &JobRef) {
        if job.borrow().cut.is_none() {
            return;
        }
        let frontier = job
            .borrow()
            .collectors
            .iter()
            .filter_map(|collector| {
                let collector = collector.borrow();
                collector
                    .active
                    .then(|| Key::of(&collector.key.0, collector.count as i64))
            })
            .max();
        if let (Some(frontier), Some(cut)) = (frontier, &mut job.borrow_mut().cut) {
            if cut.frontier.as_ref().is_none_or(|old| &frontier > old) {
                cut.frontier = Some(frontier);
            }
        }
    }
    fn checkpoint_release(&self, job: &JobRef) -> Option<CutRequest> {
        let request = job.borrow_mut().cut.take()?;
        self.checkpoint_count
            .set(self.checkpoint_count.get().saturating_sub(1));
        for inf in &request.held {
            if inf.borrow().state == InfState::Ready {
                self.ready.borrow_mut().push(inf.clone());
            }
        }
        let strands: Vec<_> = job.borrow().strands.values().cloned().collect();
        for strand in strands {
            if strand.borrow().state == StrandState::Runnable {
                job.borrow_mut().mark_runnable(&strand);
            }
        }
        self.mark_dirty(job.borrow().id);
        Some(request)
    }
    pub(crate) fn checkpoint_finish(
        &self,
        py: Python<'_>,
        job: &JobRef,
        status: &str,
        details: &Bound<'_, PyDict>,
    ) -> PyResult<()> {
        let Some(request) = self.checkpoint_release(job) else {
            return Ok(());
        };
        self.h
            .machine
            .bind(py)
            .getattr("checkpoints")?
            .call_method1("finished", (request.id, status, details))?;
        Ok(())
    }
    pub(crate) fn checkpoint_cancel(&self, py: Python<'_>, request: &str) -> PyResult<Py<PyDict>> {
        let jobs: Vec<_> = self.jobs.borrow().values().cloned().collect();
        let reply = PyDict::new(py);
        for job in jobs {
            if job
                .borrow()
                .cut
                .as_ref()
                .is_some_and(|cut| cut.id == request)
            {
                self.checkpoint_finish(py, &job, "cancelled", &PyDict::new(py))?;
                reply.set_item("status", "cancelled")?;
                return Ok(reply.unbind());
            }
        }
        reply.set_item("status", "not_found")?;
        Ok(reply.unbind())
    }
    /// Before ordinary fault squash destroys evidence of younger issued work.
    pub(crate) fn checkpoint_fault(&self, py: Python<'_>, job: &JobRef, key: &Key) -> PyResult<()> {
        if job.borrow().cut.is_none() {
            return Ok(());
        }
        let invalidated: Vec<_> = job
            .borrow()
            .window
            .live()
            .iter()
            .filter_map(|inf| {
                let inf = inf.borrow();
                let protected = !inf.attempt_ids.is_empty()
                    || (inf.state == InfState::Done
                        && !inf.control
                        && inf.fault.is_none()
                        && matches!(
                            inf.resolution,
                            "content_hit" | "positional_hit" | "squash_reuse"
                        ));
                (inf.key > *key && protected).then(|| inf.key.clone())
            })
            .collect();
        if !invalidated.is_empty() {
            let details = PyDict::new(py);
            details.set_item("fault_key", key.to_py(py))?;
            details.set_item(
                "invalidated",
                PyList::new(py, invalidated.iter().map(|key| key.to_py(py)))?,
            )?;
            self.checkpoint_finish(py, job, "fault_interrupted", &details)?;
        }
        Ok(())
    }
    pub(crate) fn checkpoint_deadline(&self) -> Option<f64> {
        if self.checkpoint_count.get() == 0 {
            return None;
        }
        self.jobs
            .borrow()
            .values()
            .filter_map(|job| job.borrow().cut.as_ref().map(|cut| cut.deadline))
            .min_by(f64::total_cmp)
    }

    fn checkpoint_candidates(&self, job: &JobRef) -> Vec<Candidate> {
        let job = job.borrow();
        let mut candidates = Vec::new();
        for inf in job.window.live() {
            let inf = inf.borrow();
            if matches!(inf.state, InfState::Waiting | InfState::Ready)
                && !inf.control
                && inf.attempt_ids.is_empty()
            {
                if let Some(snapshot) = &inf.snapshot {
                    let key = inf.key.clone();
                    if let Some((&counter, prefix)) = key.0.split_last() {
                        candidates.push(Candidate {
                            pc: inf.ins.addr,
                            prefix: Rc::from(prefix),
                            counter,
                            key,
                            frames: snapshot.frames.clone(),
                            names: snapshot.names.clone(),
                        });
                    }
                }
            }
        }
        for strand in job.strands.values() {
            let strand = strand.borrow();
            if strand.spawn.is_some() || strand.state == StrandState::Ended {
                continue;
            }
            let Some(names) = &strand.names else { continue };
            let mut candidate = Candidate {
                key: strand.next_key(),
                pc: strand.pc,
                prefix: strand.prefix.clone(),
                counter: strand.counter,
                frames: strand.frames.clone(),
                names: names.snapshot(),
            };
            if strand.resume == Resume::Parked {
                if let Some(frame) = candidate.frames.last().filter(|frame| frame.join.is_some()) {
                    if let (Some(pc), Some((prefix, counter))) = (frame.branch, &frame.saved) {
                        candidate.pc = pc;
                        candidate.prefix = prefix.clone();
                        candidate.counter = counter - 1;
                        candidate.key = Key::of(prefix, counter - 1);
                        candidate.frames =
                            Rc::new(candidate.frames[..candidate.frames.len() - 1].to_vec());
                    }
                }
            }
            candidates.push(candidate);
        }
        if job.strands.is_empty() {
            if let (Some(names), Some(key)) = (&job.final_names, &job.final_key) {
                if let Some(region) = self.code.borrow().region_of(job.entry.addr) {
                    let code = self.code.borrow();
                    if let Some(pc) = (region.start..region.end).find(|addr| {
                        code.instr(*addr)
                            .is_some_and(|ins| ins.op == crate::program::core::Op::End)
                    }) {
                        if let Some((&counter, prefix)) = key.0.split_last() {
                            candidates.push(Candidate {
                                key: key.clone(),
                                pc,
                                prefix: Rc::from(prefix),
                                counter,
                                frames: Rc::new(vec![]),
                                names: names.clone(),
                            });
                        }
                    }
                }
            }
        }
        candidates.sort_by(|a, b| a.key.cmp(&b.key));
        candidates
    }

    fn checkpoint_cut(&self, py: Python<'_>, job: &JobRef) -> PyResult<bool> {
        // Expose one actual iteration when the earliest frontier is a lazy spawner;
        // no body instruction runs merely to create its representable continuation.
        let first = job
            .borrow()
            .strands
            .values()
            .filter(|s| s.borrow().state != StrandState::Ended)
            .min_by(|a, b| a.borrow().next_key().cmp(&b.borrow().next_key()))
            .cloned();
        if let Some(first) = first {
            let is_spawn = first.borrow().spawn.is_some();
            let room = {
                let job = job.borrow();
                let cut = job.cut.as_ref().expect("request");
                job.steps
                    .saturating_sub(cut.start_steps)
                    .saturating_add(cut.extra_steps)
                    < cut.step_budget
            };
            if is_spawn && !job.borrow().paused && room {
                let key = first.borrow().next_key();
                let earliest = job
                    .borrow()
                    .window
                    .first()
                    .is_none_or(|inf| inf.borrow().key >= key);
                if earliest {
                    self.spawn_next(Some(py), job, &first)?;
                }
            }
        }
        if !job.borrow().collectors.is_empty() {
            return Ok(false);
        }
        let Some(candidate) = self.checkpoint_candidates(job).into_iter().next() else {
            if job.borrow().strands.is_empty() && job.borrow().window.is_empty() {
                self.checkpoint_finish(py, job, "terminal", &PyDict::new(py))?;
                return Ok(true);
            }
            return Ok(false);
        };
        {
            let job = job.borrow();
            let cut = job.cut.as_ref().expect("active request");
            if cut
                .frontier
                .as_ref()
                .is_some_and(|frontier| candidate.key <= *frontier)
            {
                return Ok(false);
            }
            if job.window.live().iter().any(|inf| {
                let inf = inf.borrow();
                inf.key < candidate.key
                    || matches!(inf.state, InfState::Issued | InfState::Done)
                    || !inf.attempt_ids.is_empty()
            }) {
                return Ok(false);
            }
            if job.strands.values().any(|strand| {
                let strand = strand.borrow();
                strand.state != StrandState::Ended && strand.next_key() < candidate.key
            }) {
                return Ok(false);
            }
        }
        let mut ready = IndexMap::new();
        let values = PyDict::new(py);
        for (name, slot) in candidate.names.to_dict() {
            let slot = effective(&slot, &candidate.key);
            let value = {
                let slot = slot.borrow();
                if slot.state != SlotState::Ready {
                    return Ok(false);
                }
                slot.value
                    .as_ref()
                    .map(|v| v.clone_ref(py))
                    .unwrap_or_else(|| py.None())
            };
            values.set_item(self.code.borrow().name_py(py, name), value.bind(py))?;
            ready.insert(name, slot);
        }
        let frames_json = PyList::empty(py);
        let mut frames = Vec::new();
        for frame in candidate.frames.iter() {
            if frame.collector.is_some() {
                return Ok(false);
            }
            let mut normalized = frame.copy();
            if frame.join.is_some() && frame.kind == FrameKind::Iter {
                normalized.reconv = frame
                    .each
                    .and_then(|addr| self.code.borrow().instr(addr))
                    .and_then(|ins| ins.reconv);
            }
            normalized.join = None;
            let data = PyDict::new(py);
            data.set_item("reconv", normalized.reconv)?;
            data.set_item(
                "saved",
                normalized
                    .saved
                    .as_ref()
                    .map(|(prefix, counter)| (prefix_to_py(py, prefix), *counter)),
            )?;
            data.set_item("branch", normalized.branch)?;
            data.set_item("kind", normalized.kind.name())?;
            data.set_item("each", normalized.each)?;
            data.set_item("items", normalized.items.as_ref().map(|items| items.bind(py)))?;
            data.set_item("i", normalized.i)?;
            data.set_item("base", normalized.base.as_ref().map(|key| key.to_py(py)))?;
            data.set_item("joined", false)?;
            frames_json.append(data)?;
            frames.push(Rc::new(normalized));
        }
        let (entry, request, frontier, start_steps) = {
            let job = job.borrow();
            let request = job.cut.as_ref().expect("request");
            (
                job.entry.py.clone_ref(py),
                request.id.clone(),
                request.frontier.clone(),
                request.start_steps,
            )
        };
        let raw = PyDict::new(py);
        raw.set_item("pc", candidate.pc)?;
        raw.set_item("prefix", prefix_to_py(py, &candidate.prefix))?;
        raw.set_item("counter", candidate.counter)?;
        raw.set_item("frames", frames_json)?;
        raw.set_item("values", values)?;
        {
            let job = job.borrow();
            raw.set_item("job_execution_id", &job.job_execution_id)?;
            raw.set_item("stats", job.stats.to_dict(py)?)?;
            raw.set_item("steps", job.steps)?;
            raw.set_item("record", job.record.as_ref().map(|r| r.bind(py)))?;
        }
        let manifest = self.snapshot_execution(py, job)?;
        let mode = self
            .h
            .machine
            .bind(py)
            .getattr("persistence_compatibility")?;
        let decision = py
            .import("togeari.core.program.execution")?
            .getattr("compatibility")?
            .call1((manifest.bind(py), manifest.bind(py), mode))?
            .cast_into::<PyDict>()?;
        if !decision
            .get_item("allow")?
            .is_some_and(|v| v.is_truthy().unwrap_or(false))
        {
            self.checkpoint_finish(py, job, "incompatible", &decision)?;
            return Ok(true);
        }
        // Validate and serialize before any strand/window cleanup.
        let blob = self
            .h
            .snapshot_encode
            .bind(py)
            .call1((raw, entry.bind(py), self.h.code.bind(py), manifest.bind(py)))?
            .unbind();
        let info = PyDict::new(py);
        info.set_item("key", candidate.key.to_py(py))?;
        info.set_item(
            "pc",
            candidate.pc
                - self
                    .code
                    .borrow()
                    .region_of(candidate.pc)
                    .expect("region")
                    .start,
        )?;
        info.set_item("frontier", frontier.as_ref().map(|k| k.to_py(py)))?;
        {
            let job = job.borrow();
            info.set_item("retired", job.stats.retired)?;
            info.set_item(
                "steps",
                job.steps
                    .saturating_sub(start_steps)
                    .saturating_add(job.cut.as_ref().map_or(0, |cut| cut.extra_steps)),
            )?;
            info.set_item("job_execution_id", &job.job_execution_id)?;
            info.set_item("job_key", job.key.bind(py))?;
            info.set_item("entry", job.entry.py_name.bind(py))?;
            info.set_item("program_hash", job.entry.region_hash.bind(py))?;
            info.set_item("inputs_digest", job.inputs_dig.as_ref().map(|v| v.bind(py)))?;
        }
        let expired = self.now(py)? >= job.borrow().cut.as_ref().expect("request").deadline;
        if expired {
            self.checkpoint_finish(py, job, "timeout", &PyDict::new(py))?;
            return Ok(true);
        }
        let removed = job.borrow_mut().window.clear();
        let ids: HashSet<_> = removed.iter().map(|inf| inf.borrow().id).collect();
        for inf in removed {
            self.squash(py, &inf)?;
        }
        self.ready
            .borrow_mut()
            .retain(|inf| !ids.contains(&inf.borrow().id));
        self.blocked.borrow_mut().remove(&job.borrow().id);
        let old: Vec<_> = job.borrow().strands.values().cloned().collect();
        {
            let mut job = job.borrow_mut();
            for strand in old {
                job.end_strand(&strand);
            }
            job.runnable.clear();
            job.held.clear();
            job.sheap.clear();
            job.wheap.clear();
            job.final_names = None;
            job.final_key = None;
        }
        let strand = Strand::new(
            job,
            job.borrow().id,
            candidate.pc,
            candidate.prefix,
            candidate.counter,
            Rc::new(frames),
            Some(NameMap::with_base(ready)),
            None,
        );
        job.borrow_mut().add_strand(&strand);
        self.checkpoint_release(job);
        self.h
            .machine
            .bind(py)
            .getattr("checkpoints")?
            .call_method1("captured", (request, blob, info))?;
        Ok(true)
    }

    pub(crate) fn checkpoint_progress(&self, py: Python<'_>) -> PyResult<()> {
        if self.checkpoint_count.get() == 0 {
            return Ok(());
        }
        let jobs: Vec<_> = self
            .jobs
            .borrow()
            .values()
            .filter(|job| job.borrow().cut.is_some())
            .cloned()
            .collect();
        if jobs.is_empty() {
            return Ok(());
        }
        for job in jobs {
            let now = self.now(py)?;
            let status = {
                let job = job.borrow();
                let cut = job.cut.as_ref().expect("cut");
                if job.ended {
                    Some("terminal")
                } else if now >= cut.deadline {
                    Some("timeout")
                } else {
                    None
                }
            };
            if let Some(status) = status {
                self.checkpoint_finish(py, &job, status, &PyDict::new(py))?;
                continue;
            }
            self.checkpoint_extend_collectors(&job);
            match self.checkpoint_cut(py, &job) {
                Ok(true) => {}
                Ok(false) => {
                    let status = {
                        let job = job.borrow();
                        let cut = job.cut.as_ref().expect("request");
                        if job.paused {
                            Some("needs_resume")
                        } else if job
                            .steps
                            .saturating_sub(cut.start_steps)
                            .saturating_add(cut.extra_steps)
                            >= cut.step_budget
                        {
                            Some("budget_exhausted")
                        } else {
                            None
                        }
                    };
                    if let Some(status) = status {
                        self.checkpoint_finish(py, &job, status, &PyDict::new(py))?;
                    }
                }
                Err(error) if is_exception(py, &error) => {
                    let details = PyDict::new(py);
                    details.set_item("error", error.to_string())?;
                    self.checkpoint_finish(py, &job, "failed", &details)?;
                }
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }
}
