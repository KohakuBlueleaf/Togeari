//! Admission (spec/05 §5.1): the builtin rules natively, their settings read live; any other
//! rule called back with a pool view and the signals.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use crate::runtime::builtins::Tag;
use crate::runtime::core::Core;
use crate::runtime::host::{is_exception, K};

/// What a rule reads: the pool, and the lanes' signals computed on first read.
struct Reads<'py> {
    shell: Bound<'py, PyAny>,
    jobs: i64,
    bytes: i64,
    lanes: Option<Bound<'py, PyDict>>,
    py_signals: Option<Bound<'py, PyAny>>,
    pool: Option<Bound<'py, PyAny>>,
}

impl<'py> Reads<'py> {
    fn signals(&mut self, core: &Core) -> PyResult<Bound<'py, PyAny>> {
        if self.py_signals.is_none() {
            let s = core.h.signals.bind(self.shell.py()).call1((&self.shell,))?;
            self.py_signals = Some(s);
        }
        Ok(self.py_signals.clone().expect("signals"))
    }

    fn pool(&mut self, core: &Core) -> PyResult<Bound<'py, PyAny>> {
        if self.pool.is_none() {
            let p = core
                .h
                .pool_view
                .bind(self.shell.py())
                .call1((self.jobs, self.bytes))?;
            self.pool = Some(p);
        }
        Ok(self.pool.clone().expect("a pool view"))
    }
}

impl Core {
    fn lane_signals<'py>(
        &self,
        py: Python<'py>,
        reads: &mut Reads<'py>,
    ) -> PyResult<Bound<'py, PyDict>> {
        if let Some(s) = &reads.py_signals {
            return Ok(s.get_item("lanes")?.cast_into::<PyDict>()?);
        }
        if let Some(l) = &reads.lanes {
            return Ok(l.clone());
        }
        let d = PyDict::new(py);
        for lane in self.h.lanes.bind(py).try_iter()? {
            let lane = lane?;
            d.set_item(
                lane.getattr(intern!(py, "name"))?,
                lane.call_method0(intern!(py, "signals"))?,
            )?;
        }
        reads.lanes = Some(d.clone());
        Ok(d)
    }

    /// ``rule.allows(pool, signals)``: a builtin body natively, its settings read now.
    fn allows<'py>(
        &self,
        py: Python<'py>,
        rule: &Bound<'py, PyAny>,
        reads: &mut Reads<'py>,
    ) -> PyResult<bool> {
        let jobs = reads.jobs.into_pyobject(py)?;
        match self.h.builtins.method(py, rule, "allows")? {
            Some(Tag::FixedAllows) => jobs.lt(rule.getattr(intern!(py, "n"))?),
            Some(Tag::MemoryBoundAllows) => reads
                .bytes
                .into_pyobject(py)?
                .lt(rule.getattr(intern!(py, "max_bytes"))?),
            Some(Tag::TargetLoadAllows) => {
                if jobs.ge(rule.getattr(intern!(py, "max_jobs"))?)? {
                    return Ok(false);
                }
                let lane = rule.getattr(intern!(py, "lane"))?;
                let name = if lane.is_instance_of::<PyString>() {
                    lane
                } else {
                    lane.getattr(intern!(py, "name"))?
                };
                let lanes = self.lane_signals(py, reads)?;
                match lanes.call_method1(intern!(py, "get"), (name,))? {
                    info if info.is_none() => Ok(true),
                    info => info
                        .get_item("waiting")?
                        .add(info.get_item("queue")?)?
                        .lt(rule.getattr(intern!(py, "waiting"))?),
                }
            }
            Some(Tag::AllAllows) => {
                for r in rule.getattr(intern!(py, "rules"))?.try_iter()? {
                    if !self.allows(py, &r?, reads)? {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            _ => {
                let pool = reads.pool(self)?;
                let sig = reads.signals(self)?;
                rule.call_method1(intern!(py, "allows"), (pool, sig))?
                    .is_truthy()
            }
        }
    }

    pub fn admit(&self, py: Python<'_>) -> PyResult<()> {
        let shell = self.shell(py)?;
        if shell.getattr(intern!(py, "source_buf"))?.is_none()
            || shell.getattr(intern!(py, "stopping"))?.is_truthy()?
            || shell.getattr(intern!(py, "sink_pending"))?.is_truthy()?
        {
            return Ok(());
        }
        let rule = self.h.knob(py, K::Admission)?;
        let vote = self.h.has_hook(py, "vote_admit")?;
        loop {
            self.release_freed();
            if !self.jobs.borrow().is_empty() {
                let mut reads = Reads {
                    shell: shell.clone(),
                    jobs: self.jobs.borrow().len() as i64,
                    bytes: self.pool_bytes.get(),
                    lanes: None,
                    py_signals: None,
                    pool: None,
                };
                let allowed = match self.allows(py, &rule, &mut reads) {
                    Ok(a) => a,
                    Err(e) if is_exception(py, &e) => {
                        self.policy_failed(py, "admission", &e);
                        return Ok(());
                    }
                    Err(e) => return Err(e),
                };
                if !allowed {
                    return Ok(());
                }
                if vote {
                    let sig = reads.signals(self)?;
                    let ok = self
                        .h
                        .plugins
                        .bind(py)
                        .call_method1(intern!(py, "vote"), ("vote_admit", sig))?;
                    if !ok.is_truthy()? {
                        return Ok(());
                    }
                }
            }
            let item = shell.call_method0(intern!(py, "take_source"))?;
            if item.is_none() {
                return Ok(());
            }
            *self.last_started.borrow_mut() = None;
            match self.start_job(py, &item, None) {
                Ok(()) => {}
                Err(e) if is_exception(py, &e) => {
                    let key = item.getattr(intern!(py, "key"))?.str()?.to_string();
                    self.h
                        .log(py, "error", &format!("job {key} could not start"), Some(&e));
                    self.stats.borrow_mut().machine_errors += 1;
                    let t = e.get_type(py).name()?.to_string();
                    let msg = format!("{t}: {}", e.value(py).str()?);
                    self.reject(py, &item, "MachineError", &msg, None, None)?;
                    continue;
                }
                Err(e) => return Err(e),
            }

            // The new job fetches and issues at once: the next check counts its load
            let job = self.last_started.borrow().clone();
            if let Some(job) = job {
                let caps = self.caps(py)?;
                let mut pass = Core::pass(&job);
                match self.run_pass(Some(py), &job, &mut pass, &caps) {
                    Ok(_) => {}
                    Err(e) if is_exception(py, &e) => self.job_error(py, &job, &e)?,
                    Err(e) => return Err(e),
                }
                let issue = self.issue_now(py)?;
                self.issue(py, &issue, None)?;
            }
        }
    }

    /// Resident jobs and the statements in their windows.
    pub fn window_total(&self) -> usize {
        self.jobs
            .borrow()
            .values()
            .map(|j| j.borrow().window.len())
            .sum()
    }
}
