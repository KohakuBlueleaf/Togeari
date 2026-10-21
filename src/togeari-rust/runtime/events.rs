//! Bus events from the core, built only when the bus wants them; and the error kinds faults
//! and results carry.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use super::core::{Core, Pend};
use super::state::{InfRef, Obj};

/// Why a fetch step stopped: a KIR runtime error (a fault the strand takes), or a Python error.
pub enum Stop {
    Fault(String),
    Err(PyErr),
}

/// The GIL token of a step that may need Python; only a step run with the GIL reaches it.
pub fn gil(py: Option<Python<'_>>) -> Python<'_> {
    py.expect("a step that needs Python runs with the GIL")
}

impl From<PyErr> for Stop {
    fn from(e: PyErr) -> Self {
        Stop::Err(e)
    }
}

/// The error a statement faults with.
pub enum Exc {
    // A KIR runtime error (never catchable)
    Runtime(String),
    // An error of a named builtin type
    Named(&'static str, String),
    // A Python exception object
    Obj(Obj),
}

/// What a statement completes with.
pub enum Res {
    Ok(Obj, bool),
    ItemError(Obj),
    UnitError(Obj),
    Cancelled,
    Other(Obj),
}

impl Core {
    pub fn res_of(&self, r: &Bound<'_, PyAny>) -> PyResult<Res> {
        let py = r.py();
        let h = &self.h;
        Ok(if r.is_instance(h.ok.bind(py))? {
            let v = r.getattr(intern!(py, "value"))?.unbind();
            Res::Ok(v, r.getattr(intern!(py, "finished"))?.is_truthy()?)
        } else if r.is_instance(h.item_error.bind(py))? {
            Res::ItemError(r.getattr(intern!(py, "error"))?.unbind())
        } else if r.is_instance(h.unit_error.bind(py))? {
            Res::UnitError(r.getattr(intern!(py, "error"))?.unbind())
        } else if r.is_instance(h.cancelled.bind(py))? {
            Res::Cancelled
        } else {
            Res::Other(r.clone().unbind())
        })
    }

    pub fn res_to_py(&self, py: Python<'_>, r: &Res) -> PyResult<Obj> {
        let h = &self.h;
        Ok(match r {
            Res::Ok(v, finished) => h.ok.bind(py).call1((v.bind(py), *finished))?.unbind(),
            Res::ItemError(e) => h.item_error.bind(py).call1((e.bind(py),))?.unbind(),
            Res::UnitError(e) => h.unit_error.bind(py).call1((e.bind(py),))?.unbind(),
            Res::Cancelled => h.cancelled.bind(py).call0()?.unbind(),
            Res::Other(o) => o.clone_ref(py),
        })
    }

    /// Whether a listener or a subscriber receives an event of ``kind`` (of ``job``) now.
    pub fn wants(&self, py: Python<'_>, kind: &str, job: Option<u64>) -> PyResult<bool> {
        if !self.h.bus_listeners.bind(py).is_empty() {
            return Ok(true);
        }
        let by_kind = self.h.bus_by_kind.bind(py);
        if !by_kind.is_empty() && by_kind.contains(kind)? {
            return Ok(true);
        }
        if self.h.bus_subs.bind(py).is_empty() {
            return Ok(false);
        }
        self.h
            .bus
            .bind(py)
            .call_method1(intern!(py, "wants"), (kind, job))?
            .is_truthy()
    }

    pub fn emit(&self, py: Python<'_>, kind: &str, fields: &Bound<'_, PyDict>) -> PyResult<()> {
        self.h
            .bus
            .bind(py)
            .call_method(intern!(py, "emit"), (kind,), Some(fields))?;
        Ok(())
    }

    /// A statement event: job, node, address, key and lane, then ``extra``.
    pub fn emit_stmt(
        &self,
        py: Python<'_>,
        kind: &str,
        inf: &InfRef,
        extra: Option<(&str, Obj)>,
    ) -> PyResult<()> {
        let (job, ins, key) = {
            let x = inf.borrow();
            (x.job_id, x.ins.clone(), x.key.clone())
        };
        let lane: Option<&Py<PyString>> = match &ins.binding {
            Some(b) if !b.inline => b.lane_name.as_ref(),
            _ => None,
        };
        let f = PyDict::new(py);
        f.set_item(intern!(py, "job"), job)?;
        f.set_item(intern!(py, "node"), ins.node.bind(py))?;
        f.set_item(intern!(py, "addr"), ins.addr)?;
        f.set_item(intern!(py, "key"), key.to_py(py))?;
        f.set_item(intern!(py, "lane"), lane.map(|l| l.bind(py)))?;
        if let Some((k, v)) = extra {
            f.set_item(k, v)?;
        }
        self.emit(py, kind, &f)
    }

    /// ``emit_stmt`` when the bus wants ``kind`` for the statement's job.
    pub fn stmt_event(&self, py: Python<'_>, kind: &str, inf: &InfRef) -> PyResult<()> {
        let jid = inf.borrow().job_id;
        if self.wants(py, kind, Some(jid))? {
            self.emit_stmt(py, kind, inf, None)?;
        }
        Ok(())
    }

    /// A statement event now (the GIL held), or kept for the end of a GIL-free region.
    pub fn note_stmt(
        &self,
        py: Option<Python<'_>>,
        kind: &'static str,
        inf: &InfRef,
    ) -> PyResult<()> {
        match py {
            Some(py) => self.stmt_event(py, kind, inf),
            None => {
                if !self.bus_quiet.get() {
                    self.pending
                        .borrow_mut()
                        .push(Pend::Stmt { kind, inf: inf.clone() });
                }
                Ok(())
            }
        }
    }

    /// A strand event (``job``, ``strand``, ``addr`` and, on a park, ``cont``), now or kept.
    pub fn note_strand(
        &self,
        py: Option<Python<'_>>,
        kind: &'static str,
        job: u64,
        strand: u64,
        addr: i64,
        cont: Option<u64>,
    ) -> PyResult<()> {
        match py {
            Some(py) => self.strand_event(py, kind, job, strand, addr, cont),
            None => {
                if !self.bus_quiet.get() {
                    self.pending
                        .borrow_mut()
                        .push(Pend::Strand { kind, job, strand, addr, cont });
                }
                Ok(())
            }
        }
    }

    fn strand_event(
        &self,
        py: Python<'_>,
        kind: &str,
        job: u64,
        strand: u64,
        addr: i64,
        cont: Option<u64>,
    ) -> PyResult<()> {
        if !self.wants(py, kind, Some(job))? {
            return Ok(());
        }
        let f = PyDict::new(py);
        f.set_item(intern!(py, "job"), job)?;
        f.set_item(intern!(py, "strand"), strand)?;
        f.set_item(intern!(py, "addr"), addr)?;
        if let Some(c) = cont {
            f.set_item(intern!(py, "cont"), c)?;
        }
        self.emit(py, kind, &f)
    }

    /// The events a GIL-free region met, in order.
    pub fn flush(&self, py: Python<'_>) -> PyResult<()> {
        let pending = std::mem::take(&mut *self.pending.borrow_mut());
        for p in pending {
            match p {
                Pend::Stmt { kind, inf } => self.stmt_event(py, kind, &inf)?,
                Pend::Strand { kind, job, strand, addr, cont } => {
                    self.strand_event(py, kind, job, strand, addr, cont)?
                }
            }
        }
        Ok(())
    }

    /// Whether nothing on the bus receives any event (then a region keeps none).
    pub fn quiet(&self, py: Python<'_>) -> bool {
        self.h.bus_listeners.bind(py).is_empty()
            && self.h.bus_by_kind.bind(py).is_empty()
            && self.h.bus_subs.bind(py).is_empty()
    }
}
