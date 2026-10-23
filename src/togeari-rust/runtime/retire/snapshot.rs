//! Snapshot v3 uses the shared Python codec for identity, shape and relocation checks.
//! Native conversion is detached: no job or name-interner mutation until it succeeds.

use std::rc::Rc;

use indexmap::IndexMap;
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyString, PyTuple};

use crate::runtime::core::Core;
use crate::runtime::state::job::JobStats;
use crate::runtime::state::key::prefix_to_py;
use crate::runtime::state::{
    effective, Frame, FrameKind, JobRef, Key, NameMap, Resume, Slot, SlotRef, SlotState, Strand,
};

fn required<'py>(state: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    state
        .get_item(key)?
        .ok_or_else(|| PyKeyError::new_err(format!("snapshot field {key:?} missing")))
}

fn tuple_ints(value: &Bound<'_, PyAny>) -> PyResult<Rc<[i64]>> {
    let tuple = value.cast::<PyTuple>()?;
    tuple
        .iter()
        .map(|item| item.extract::<i64>())
        .collect::<PyResult<Vec<_>>>()
        .map(Into::into)
}

fn optional_int(state: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<i64>> {
    let value = required(state, key)?;
    if value.is_none() {
        Ok(None)
    } else {
        value.extract().map(Some)
    }
}

/// Fully converted native state. Strings are not interned until this is complete.
struct PreparedSnapshot {
    job_execution_id: String,
    pc: i64,
    prefix: Rc<[i64]>,
    counter: i64,
    frames: Rc<Vec<Rc<Frame>>>,
    values: Vec<(String, Py<PyAny>)>,
    stats: JobStats,
    steps: i64,
    record: Option<Py<PyList>>,
}

impl PreparedSnapshot {
    fn decode(
        py: Python<'_>,
        job: &JobRef,
        state: &Bound<'_, PyDict>,
        decision: &Bound<'_, PyDict>,
    ) -> PyResult<Self> {
        if !required(decision, "allow")?.extract::<bool>()? {
            return Err(PyValueError::new_err("cannot apply a rejected snapshot"));
        }
        let job_execution_id: String = required(state, "job_execution_id")?.extract()?;
        let pc = required(state, "pc")?.extract()?;
        let prefix = tuple_ints(&required(state, "prefix")?)?;
        let counter = required(state, "counter")?.extract()?;
        let steps = required(state, "steps")?.extract()?;
        let mut frames = Vec::new();
        for frame in required(state, "frames")?.try_iter()? {
            let frame = frame?.cast_into::<PyDict>()?;
            let kind: String = required(&frame, "kind")?.extract()?;
            let kind = match kind.as_str() {
                "branch" => FrameKind::Branch,
                "iter" => FrameKind::Iter,
                _ => return Err(PyValueError::new_err("invalid snapshot frame kind")),
            };
            let saved = required(&frame, "saved")?;
            let saved = if saved.is_none() {
                None
            } else {
                let pair = saved.cast::<PyTuple>()?;
                if pair.len() != 2 {
                    return Err(PyValueError::new_err("invalid snapshot saved key"));
                }
                Some((tuple_ints(&pair.get_item(0)?)?, pair.get_item(1)?.extract()?))
            };
            let base = required(&frame, "base")?;
            let base = if base.is_none() {
                None
            } else {
                Some(Key(tuple_ints(&base)?))
            };
            let items = required(&frame, "items")?;
            let items = if items.is_none() {
                None
            } else {
                Some(Rc::new(items.cast_into::<PyList>()?.unbind()))
            };
            // The codec already relocated all fields and normalized joined iteration reconvergence.
            frames.push(Rc::new(Frame {
                reconv: optional_int(&frame, "reconv")?,
                saved,
                join: None,
                branch: optional_int(&frame, "branch")?,
                kind,
                each: optional_int(&frame, "each")?,
                items,
                i: required(&frame, "i")?.extract()?,
                base,
                collector: None,
            }));
        }
        let values = required(state, "values")?
            .cast_into::<PyDict>()?
            .iter()
            .map(|(name, value)| Ok((name.extract::<String>()?, value.unbind())))
            .collect::<PyResult<Vec<_>>>()?;
        let (mut stats, recording, execution, execution_epoch) = {
            let job = job.borrow();
            (
                job.stats.snapshot(py),
                job.record.is_some(),
                job.stats.extra.get("execution").map(|v| v.clone_ref(py)),
                job.stats
                    .extra
                    .get("execution_epoch")
                    .map(|v| v.clone_ref(py)),
            )
        };
        stats.update(&required(state, "stats")?.cast_into::<PyDict>()?)?;
        stats.extra.insert(
            "job_execution_id".into(),
            PyString::new(py, &job_execution_id).into_any().unbind(),
        );
        stats
            .extra
            .insert("identity_version".into(), 1_i64.into_pyobject(py)?.into_any().unbind());
        // Saved epochs are machine-local and never replace this admission's identity.
        stats.extra.shift_remove("execution");
        stats.extra.shift_remove("execution_epoch");
        // Cached result-persistence decisions belong to the source machine only.
        stats.extra.shift_remove("_persistence_context");
        if let Some(execution) = execution {
            stats.extra.insert("execution".into(), execution);
        }
        if let Some(epoch) = execution_epoch {
            stats.extra.insert("execution_epoch".into(), epoch);
        }
        stats
            .extra
            .insert("resumed".into(), PyBool::new(py, true).to_owned().into_any().unbind());
        let persistence = PyDict::new(py);
        persistence.set_item("status", required(decision, "status")?)?;
        persistence.set_item("issues", required(decision, "issues")?)?;
        stats
            .extra
            .insert("snapshot_restore".into(), persistence.into_any().unbind());
        // Validate the complete record field even when this admission did not request recording.
        let record = required(state, "record")?;
        let record = if record.is_none() {
            None
        } else {
            Some(record.cast_into::<PyList>()?.unbind())
        };
        let record = if recording { record } else { None };
        if record.is_none() {
            // Record-aligned metadata cannot outlive a record omitted at this admission.
            stats.extra.shift_remove("record_invocations");
            stats.extra.shift_remove("record_origins");
        }
        if recording && record.is_none() {
            stats.extra.insert(
                "record_skipped".into(),
                PyString::new(py, "resumed without a record")
                    .into_any()
                    .unbind(),
            );
        }
        Ok(Self {
            job_execution_id,
            pc,
            prefix,
            counter,
            frames: Rc::new(frames),
            values,
            stats,
            steps,
            record,
        })
    }
}

impl Core {
    /// Current expected identity always comes from the admitted entry, never the blob.
    pub(crate) fn snapshot_execution(&self, py: Python<'_>, job: &JobRef) -> PyResult<Py<PyAny>> {
        let (entry, epoch) = {
            let job = job.borrow();
            (
                job.entry.py.clone_ref(py),
                job.stats
                    .extra
                    .get("execution_epoch")
                    .map(|v| v.clone_ref(py))
                    .unwrap_or_else(|| py.None()),
            )
        };
        let region = entry.bind(py).getattr("region")?;
        Ok(self
            .h
            .execution_context
            .bind(py)
            .call1((region, self.h.code.bind(py), self.h.machine.bind(py), epoch.bind(py)))?
            .unbind())
    }

    /// Capture only the existing retire boundary; active collect/spawn state stays unsupported.
    pub fn capture(&self, py: Python<'_>, job: &JobRef) -> PyResult<Option<Py<PyAny>>> {
        let strand = {
            let job = job.borrow();
            if !job.collectors.is_empty() || !job.window.is_empty() || job.strands.len() != 1 {
                return Ok(None);
            }
            job.strands.values().next().expect("one strand").clone()
        };
        let strand = strand.borrow();
        if strand.spawn.is_some() {
            return Ok(None);
        }
        let (mut pc, mut prefix, mut counter) = (strand.pc, strand.prefix.clone(), strand.counter);
        let mut frames_now = strand.frames.to_vec();
        if strand.resume == Resume::Parked
            && frames_now.last().is_some_and(|frame| frame.join.is_some())
        {
            let top = frames_now.pop().expect("a frame");
            pc = top
                .branch
                .ok_or_else(|| PyValueError::new_err("parked snapshot frame has no branch"))?;
            let (saved_prefix, saved_counter) = top
                .saved
                .clone()
                .ok_or_else(|| PyValueError::new_err("parked snapshot frame has no saved key"))?;
            prefix = saved_prefix;
            counter = saved_counter
                .checked_sub(1)
                .ok_or_else(|| PyValueError::new_err("snapshot counter overflow"))?;
        }
        let values = PyDict::new(py);
        let last = Key::last();
        {
            let code = self.code.borrow();
            for (name, slot) in strand.names.as_ref().expect("strand names").to_dict() {
                let slot = effective(&slot, &last);
                let slot = slot.borrow();
                if slot.state != SlotState::Ready {
                    return Ok(None);
                }
                let value = slot
                    .value
                    .as_ref()
                    .map(|value| value.clone_ref(py))
                    .unwrap_or_else(|| py.None());
                values.set_item(code.name_py(py, name), value)?;
            }
        }
        let frames = PyList::empty(py);
        for frame in &frames_now {
            let state = PyDict::new(py);
            state.set_item("reconv", frame.reconv)?;
            let saved = match &frame.saved {
                Some((prefix, counter)) => Some(PyTuple::new(
                    py,
                    [
                        prefix_to_py(py, prefix).into_any(),
                        counter.into_pyobject(py)?.into_any(),
                    ],
                )?),
                None => None,
            };
            state.set_item("saved", saved)?;
            state.set_item("branch", frame.branch)?;
            state.set_item("kind", frame.kind.name())?;
            state.set_item("each", frame.each)?;
            state.set_item("items", frame.items.as_ref().map(|items| items.bind(py)))?;
            state.set_item("i", frame.i)?;
            state.set_item("base", frame.base.as_ref().map(|base| base.to_py(py)))?;
            state.set_item("joined", frame.join.is_some())?;
            frames.append(state)?;
        }
        drop(strand);
        let state = PyDict::new(py);
        state.set_item("pc", pc)?;
        state.set_item("prefix", prefix_to_py(py, &prefix))?;
        state.set_item("counter", counter)?;
        state.set_item("frames", frames)?;
        state.set_item("values", values)?;
        let entry = {
            let job = job.borrow();
            state.set_item("job_execution_id", &job.job_execution_id)?;
            state.set_item("stats", job.stats.to_dict(py)?)?;
            state.set_item("steps", job.steps)?;
            state.set_item("record", job.record.as_ref().map(|record| record.bind(py)))?;
            job.entry.py.clone_ref(py)
        };
        let manifest = self.snapshot_execution(py, job)?;
        Ok(Some(
            self.h
                .snapshot_encode
                .bind(py)
                .call1((state, entry.bind(py), self.h.code.bind(py), manifest.bind(py)))?
                .unbind(),
        ))
    }

    /// Apply a shared-codec-approved state. Every fallible conversion finishes before commit.
    pub(crate) fn apply_prepared_snapshot(
        &self,
        py: Python<'_>,
        job: &JobRef,
        state: &Bound<'_, PyDict>,
        decision: &Bound<'_, PyDict>,
    ) -> PyResult<()> {
        let prepared = PreparedSnapshot::decode(py, job, state, decision)?;
        let jid = job.borrow().id;
        // Commit begins. No Python callbacks or fallible conversions remain below.
        let mut base: IndexMap<u32, SlotRef> = IndexMap::new();
        {
            let mut code = self.code.borrow_mut();
            for (name, value) in prepared.values {
                base.insert(code.intern(py, &name), Slot::constant(value));
            }
        }
        let strand = Strand::new(
            job,
            jid,
            prepared.pc,
            prepared.prefix,
            prepared.counter,
            prepared.frames,
            Some(NameMap::with_base(base)),
            None,
        );
        let old: Vec<_> = job.borrow().strands.values().cloned().collect();
        let mut job = job.borrow_mut();
        for old in old {
            job.end_strand(&old);
        }
        job.add_strand(&strand);
        job.job_execution_id = prepared.job_execution_id;
        job.snap_at = prepared.stats.retired;
        job.stats = prepared.stats;
        job.steps = prepared.steps;
        job.record = prepared.record;
        Ok(())
    }
}
