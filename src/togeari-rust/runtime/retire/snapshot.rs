//! Job snapshots at retire boundaries and resume from them, in the Python core's layout
//! (`togeari.core.runtime.retire.snapshot`): a snapshot either core writes, either core reads.

use std::rc::Rc;

use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyString, PyTuple};

use crate::runtime::core::Core;
use crate::runtime::state::key::prefix_to_py;
use crate::runtime::state::{
    effective, Frame, FrameKind, JobRef, Key, NameMap, Resume, Slot, SlotRef, SlotState, Strand,
};

// The layout of a captured state; restore reads only this version
const VERSION: i64 = 1;

fn tuple_ints(t: &Bound<'_, PyAny>) -> PyResult<Rc<[i64]>> {
    let mut v = Vec::new();
    for x in t.try_iter()? {
        v.push(x?.extract::<i64>()?);
    }
    Ok(v.into())
}

fn opt(d: &Bound<'_, PyDict>, k: &str) -> PyResult<Option<i64>> {
    match d.get_item(k)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract()?)),
        _ => Ok(None),
    }
}

impl Core {
    /// The job's state as bytes, or None off a boundary; raises when a value cannot pickle.
    pub fn capture(&self, py: Python<'_>, job: &JobRef) -> PyResult<Option<Py<PyAny>>> {
        let st = {
            let j = job.borrow();
            if !j.window.is_empty() || j.strands.len() != 1 {
                return Ok(None);
            }
            j.strands.values().next().expect("a strand").clone()
        };
        let s = st.borrow();
        if s.spawn.is_some() {
            return Ok(None);
        }
        let (mut pc, mut prefix, mut counter) = (s.pc, s.prefix.clone(), s.counter);
        let mut frames_now: Vec<Rc<Frame>> = s.frames.to_vec();

        // A strand parked at a branch is captured as just before that branch
        if s.resume == Resume::Parked && frames_now.last().is_some_and(|f| f.join.is_some()) {
            let top = frames_now.pop().expect("a frame");
            pc = top.branch.expect("a branch address");
            let (p, c) = top.saved.clone().expect("a saved key");
            prefix = p;
            counter = c - 1;
        }

        // Every name must hold a filled value
        let values = PyDict::new(py);
        let last = Key::last();
        {
            let code = self.code.borrow();
            for (name, slot) in s.names.as_ref().expect("a strand with names").to_dict() {
                let e = effective(&slot, &last);
                let b = e.borrow();
                if b.state != SlotState::Ready {
                    return Ok(None);
                }
                let v = b
                    .value
                    .as_ref()
                    .map(|v| v.clone_ref(py))
                    .unwrap_or_else(|| py.None());
                values.set_item(code.name_py(py, name), v)?;
            }
        }

        let frames = PyList::empty(py);
        for f in &frames_now {
            let d = PyDict::new(py);
            d.set_item("reconv", f.reconv)?;
            let saved = match &f.saved {
                Some((p, c)) => Some(PyTuple::new(
                    py,
                    [
                        prefix_to_py(py, p).into_any(),
                        c.into_pyobject(py)?.into_any(),
                    ],
                )?),
                None => None,
            };
            d.set_item("saved", saved)?;
            d.set_item("branch", f.branch)?;
            d.set_item("kind", f.kind.name())?;
            d.set_item("each", f.each)?;
            d.set_item("items", f.items.as_ref().map(|x| x.bind(py)))?;
            d.set_item("i", f.i)?;
            d.set_item("base", f.base.as_ref().map(|b| b.to_py(py)))?;
            d.set_item("joined", f.join.is_some())?;
            frames.append(d)?;
        }
        drop(s);
        let j = job.borrow();
        let state = PyDict::new(py);
        state.set_item("version", VERSION)?;
        state.set_item("pc", pc)?;
        state.set_item("prefix", prefix_to_py(py, &prefix))?;
        state.set_item("counter", counter)?;
        state.set_item("frames", frames)?;
        state.set_item("values", values)?;
        state.set_item("stats", j.stats.to_dict(py)?)?;
        state.set_item("steps", j.steps)?;
        state.set_item("record", j.record.as_ref().map(|r| r.bind(py)))?;
        let kw = PyDict::new(py);
        kw.set_item("protocol", 5)?;
        Ok(Some(
            self.h
                .pickle_dumps
                .bind(py)
                .call((state,), Some(&kw))?
                .unbind(),
        ))
    }

    /// Put a captured state back into a fresh job; False if it cannot be read or is of another
    /// version.
    pub fn restore(&self, py: Python<'_>, job: &JobRef, blob: &Bound<'_, PyAny>) -> PyResult<bool> {
        let (jid, key) = {
            let j = job.borrow();
            (j.id, j.key.clone_ref(py))
        };
        let warn = |msg: String| {
            let _ = self.h.snap_log.bind(py).call_method1("warning", (msg,));
        };
        let state = match self.h.pickle_loads.bind(py).call1((blob,)) {
            Ok(s) => s,
            Err(e) if crate::runtime::host::is_exception(py, &e) => {
                warn(format!("snapshot of job {} unreadable; starting over", key.bind(py)));
                return Ok(false);
            }
            Err(e) => return Err(e),
        };
        let state = match state.cast_into::<PyDict>() {
            Ok(d)
                if d.get_item("version")?
                    .is_some_and(|v| v.eq(VERSION).unwrap_or(false)) =>
            {
                d
            }
            Ok(d) => {
                let got = d
                    .get_item("version")?
                    .map(|v| v.repr().map(|r| r.to_string()))
                    .transpose()?;
                warn(format!(
                    "snapshot of job {} has version {}, not {VERSION}; starting over",
                    key.bind(py),
                    got.unwrap_or_else(|| "None".into())
                ));
                return Ok(false);
            }
            Err(_) => {
                warn(format!(
                    "snapshot of job {} has version None, not {VERSION}; starting over",
                    key.bind(py)
                ));
                return Ok(false);
            }
        };

        // Frames restore without their joins; values belong to this job alone: no copy
        let mut frames = Vec::new();
        for d in state.get_item("frames")?.expect("frames").try_iter()? {
            let d = d?.cast_into::<PyDict>()?.copy()?;
            let joined = d
                .get_item("joined")?
                .map(|v| v.is_truthy())
                .transpose()?
                .unwrap_or(false);
            let kind: String = d.get_item("kind")?.expect("a kind").extract()?;
            let kind = if kind == "iter" {
                FrameKind::Iter
            } else {
                FrameKind::Branch
            };
            let each: Option<i64> = opt(&d, "each")?;
            let mut reconv: Option<i64> = opt(&d, "reconv")?;
            if joined && kind == FrameKind::Iter {
                reconv = self
                    .code
                    .borrow()
                    .instr(each.expect("an each"))
                    .and_then(|e| e.reconv);
            }
            let saved = match d.get_item("saved")? {
                Some(s) if !s.is_none() => {
                    let t = s.cast_into::<PyTuple>()?;
                    Some((tuple_ints(&t.get_item(0)?)?, t.get_item(1)?.extract()?))
                }
                _ => None,
            };
            let base = match d.get_item("base")? {
                Some(b) if !b.is_none() => Some(Key(tuple_ints(&b)?)),
                _ => None,
            };
            let items = match d.get_item("items")? {
                Some(x) if !x.is_none() => Some(Rc::new(x.cast_into::<PyList>()?.unbind())),
                _ => None,
            };
            frames.push(Rc::new(Frame {
                reconv,
                saved,
                join: None,
                branch: opt(&d, "branch")?,
                kind,
                each,
                items,
                i: opt(&d, "i")?.unwrap_or(0),
                base,
            }));
        }
        let mut base: IndexMap<u32, SlotRef> = IndexMap::new();
        {
            let mut code = self.code.borrow_mut();
            for (n, v) in state
                .get_item("values")?
                .expect("values")
                .cast_into::<PyDict>()?
                .iter()
            {
                let name: String = n.extract()?;
                base.insert(code.intern(py, &name), Slot::constant(v.unbind()));
            }
        }
        let strands: Vec<_> = job.borrow().strands.values().cloned().collect();
        for st in strands {
            job.borrow_mut().end_strand(&st);
        }
        let pc: i64 = state.get_item("pc")?.expect("a pc").extract()?;
        let prefix = tuple_ints(&state.get_item("prefix")?.expect("a prefix"))?;
        let counter: i64 = state.get_item("counter")?.expect("a counter").extract()?;
        let st = Strand::new(
            job,
            jid,
            pc,
            prefix,
            counter,
            Rc::new(frames),
            Some(NameMap::with_base(base)),
            None,
        );
        job.borrow_mut().add_strand(&st);

        let mut j = job.borrow_mut();
        j.stats.update(
            &state
                .get_item("stats")?
                .expect("stats")
                .cast_into::<PyDict>()?,
        )?;
        j.stats
            .extra
            .insert("resumed".into(), PyBool::new(py, true).to_owned().into_any().unbind());
        j.steps = match state.get_item("steps")? {
            Some(v) => v.extract()?,
            None => 0,
        };
        j.snap_at = j.stats.retired;

        // A recorded job continues the record of the run it resumes; without one it is not
        if j.record.is_some() {
            j.record = match state.get_item("record")? {
                Some(r) if !r.is_none() => Some(r.cast_into::<PyList>()?.unbind()),
                _ => None,
            };
            if j.record.is_none() {
                let msg = PyString::new(py, "resumed without a record")
                    .into_any()
                    .unbind();
                j.stats.extra.insert("record_skipped".into(), msg);
            }
        }
        Ok(true)
    }
}
