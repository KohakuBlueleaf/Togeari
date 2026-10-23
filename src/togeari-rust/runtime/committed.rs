//! Semantic committed events use the shared portable codec and asynchronous outbox.
use super::core::Core;
use super::events::Exc;
use super::state::{InfRef, JobRef};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

pub(crate) struct EventAdmission {
    pub job: JobRef,
    pub item: Py<PyAny>,
    pub inputs: Py<PyAny>,
    pub state: Py<PyDict>,
    pub decision: Py<PyDict>,
}

impl Core {
    pub(crate) fn stage_event_admission(
        &self,
        py: Python<'_>,
        job: &JobRef,
        item: &Bound<'_, PyAny>,
        inputs: &Bound<'_, PyAny>,
        state: &Bound<'_, PyDict>,
        decision: &Bound<'_, PyDict>,
    ) -> PyResult<()> {
        let (id, key) = {
            let j = job.borrow();
            (j.id, j.key.clone_ref(py))
        };
        let callback = py
            .import("functools")?
            .getattr("partial")?
            .call1((self.shell(py)?.getattr("_event_restore")?, id))?;
        let staged = EventAdmission {
            job: job.clone(),
            item: item.clone().unbind(),
            inputs: inputs.clone().unbind(),
            state: state.clone().unbind(),
            decision: decision.clone().unbind(),
        };
        self.keys.bind(py).add(key.bind(py))?;
        self.event_admissions.borrow_mut().insert(id, staged);
        let result = self.event_manager(py).and_then(|manager| {
            manager.call_method1(
                "check_frontier",
                (
                    state.get_item("job_execution_id")?.expect("saved UID"),
                    state.get_item("event_state")?.expect("saved cursor"),
                    callback,
                ),
            )
        });
        if let Err(error) = result {
            let denial = PyDict::new(py);
            denial.set_item("allow", false)?;
            denial.set_item("issues", error.to_string())?;
            self.complete_event_admission(py, id, &denial)?;
        }
        Ok(())
    }

    pub(crate) fn complete_event_admission(
        &self,
        py: Python<'_>,
        id: u64,
        result: &Bound<'_, PyDict>,
    ) -> PyResult<()> {
        let Some(staged) = self.event_admissions.borrow_mut().remove(&id) else {
            return Ok(());
        };
        let (key, handle) = {
            let j = staged.job.borrow();
            (j.key.clone_ref(py), j.handle.as_ref().map(|h| h.clone_ref(py)))
        };
        self.keys.bind(py).discard(key.bind(py))?;
        if !result
            .get_item("allow")?
            .is_some_and(|v| v.is_truthy().unwrap_or(false))
        {
            return self.reject(
                py,
                staged.item.bind(py),
                "EventJournalIncompatible",
                &result.repr()?.to_string(),
                handle.as_ref().map(|h| h.bind(py)),
                None,
            );
        }
        // Identity/frontier rejection is incompatibility; exceptions after that
        // boundary are ordinary machine admission failures, not bad artifacts.
        let failure_kind = std::cell::Cell::new("EventJournalIncompatible");
        let finish = || -> PyResult<()> {
            if self.shell(py)?.getattr("stopping")?.is_truthy()? {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "machine stopped during event restore",
                ));
            }
            let entry = staged.job.borrow().entry.py.clone_ref(py);
            let observed = self.h.observe_execution.bind(py).call1((
                entry.bind(py).getattr("region")?,
                self.h.code.bind(py),
                self.h.machine.bind(py),
            ))?;
            let current_hash: String =
                observed.get_item("manifest")?.get_item("hash")?.extract()?;
            let current_epoch: i64 = observed.get_item("epoch")?.extract()?;
            let changed = {
                let j = staged.job.borrow();
                current_hash != j.invocation_execution_hash
                    || current_epoch
                        != j.stats.extra["execution_epoch"].bind(py).extract::<i64>()?
            };
            if changed {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "execution changed during event frontier validation",
                ));
            }
            failure_kind.set("MachineError");
            self.apply_prepared_snapshot(
                py,
                &staged.job,
                staged.state.bind(py),
                staged.decision.bind(py),
            )?;
            self.finish_event_admission(py, &staged.job, staged.inputs.bind(py), true)
        };
        match finish() {
            Ok(()) => Ok(()),
            Err(error) => {
                if self.jobs.borrow().contains_key(&id) {
                    self.job_error(py, &staged.job, &error)
                } else {
                    self.reject(
                        py,
                        staged.item.bind(py),
                        failure_kind.get(),
                        &error.to_string(),
                        handle.as_ref().map(|h| h.bind(py)),
                        None,
                    )
                }
            }
        }
    }

    pub(crate) fn event_manager<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.h.machine.bind(py).getattr("events")
    }

    pub(crate) fn ensure_event_state(&self, py: Python<'_>, job: &JobRef) -> PyResult<()> {
        if job.borrow().event_state.is_some() {
            return Ok(());
        }
        let entry = job.borrow().entry.py.clone_ref(py);
        let calls = self
            .h
            .code
            .bind(py)
            .call_method1("calls", (entry.bind(py).getattr("region")?,))?;
        let mut emits = false;
        for ins in calls.try_iter()? {
            if ins?.getattr("semantic_emit")?.is_truthy()? {
                emits = true;
                break;
            }
        }
        if !emits {
            return Ok(());
        }
        let descriptor = self
            .h
            .machine
            .bind(py)
            .getattr("event_output")?
            .call_method0("descriptor")?;
        let state = PyDict::new(py);
        state.set_item("version", 1)?;
        state.set_item("next_sequence", 0)?;
        state.set_item("committed_watermark", -1)?;
        state.set_item("journal_identity", descriptor.get_item("journal")?)?;
        let sidecar = PyDict::new(py);
        sidecar.set_item("version", 1)?;
        sidecar.set_item("events", PyList::empty(py))?;
        let mut job = job.borrow_mut();
        job.event_state = Some(state.unbind());
        job.stats
            .extra
            .entry("committed_events".into())
            .or_insert_with(|| sidecar.into_any().unbind());
        Ok(())
    }

    /// Capture and validate size before any attempt or user implementation executes.
    pub(crate) fn prepare_event(&self, py: Python<'_>, job: &JobRef, inf: &InfRef) -> PyResult<()> {
        self.ensure_event_state(py, job)?;
        let (values, invocation) = {
            let x = inf.borrow();
            (
                x.values.as_ref().expect("issued values").clone_ref(py),
                x.invocation_id.clone().expect("emit identity"),
            )
        };
        let values = values.bind(py);
        let event = py
            .import("kohakunode.events")?
            .getattr("Event")?
            .call_method1(
                "capture",
                (
                    values.get_item("topic")?.expect("emit topic"),
                    values.get_item("payload")?.expect("emit payload"),
                ),
            )?;
        let (uid, sequence) = {
            let j = job.borrow();
            (
                j.job_execution_id.clone(),
                j.event_state
                    .as_ref()
                    .expect("event cursor")
                    .bind(py)
                    .get_item("next_sequence")?
                    .expect("sequence")
                    .extract::<i64>()?,
            )
        };
        let prepared = self
            .event_manager(py)?
            .call_method1("prepare", (uid, sequence, invocation, event))?;
        inf.borrow_mut().event = Some(prepared.unbind());
        Ok(())
    }

    /// False leaves the window intact. Only append ACK permits the destructive pop.
    pub(crate) fn event_before_retire(
        &self,
        py: Python<'_>,
        job: &JobRef,
        inf: &InfRef,
    ) -> PyResult<bool> {
        let event = inf
            .borrow()
            .event
            .as_ref()
            .expect("prepared emit")
            .clone_ref(py);
        let token = inf.borrow().event_commit.as_ref().map(|v| v.clone_ref(py));
        let status = match &token {
            Some(t) => t.bind(py).get_item("status")?.extract::<String>()?,
            None => "full".into(),
        };
        let token = if status == "full" {
            let wake = py
                .import("functools")?
                .getattr("partial")?
                .call1((self.shell(py)?.getattr("_event_wake")?, job.borrow().id))?;
            let token = self
                .event_manager(py)?
                .call_method1("commit", (event.bind(py), wake))?
                .unbind();
            inf.borrow_mut().event_commit = Some(token.clone_ref(py));
            token
        } else {
            token.expect("existing token")
        };
        match token
            .bind(py)
            .get_item("status")?
            .extract::<String>()?
            .as_str()
        {
            "committed" => Ok(true),
            "failed" => {
                self.fault(
                    py,
                    inf,
                    Exc::Runtime(format!("EventCommitFailed: {}", token.bind(py).repr()?)),
                    false,
                )?;
                Ok(false)
            }
            _ => Ok(false),
        }
    }

    pub(crate) fn event_retired(&self, py: Python<'_>, job: &JobRef, inf: &InfRef) -> PyResult<()> {
        let event = inf
            .borrow()
            .event
            .as_ref()
            .expect("committed emit")
            .clone_ref(py);
        let sequence: i64 = event.bind(py).getattr("sequence")?.extract()?;
        let envelope = event.bind(py).call_method0("to_dict")?;
        let sidecar = {
            let mut j = job.borrow_mut();
            let state = j.event_state.as_ref().expect("event cursor").bind(py);
            state.set_item("next_sequence", sequence + 1)?;
            state.set_item("committed_watermark", sequence)?;
            if !j.stats.extra.contains_key("committed_events") {
                let sidecar = PyDict::new(py);
                sidecar.set_item("version", 1)?;
                sidecar.set_item("events", PyList::empty(py))?;
                j.stats
                    .extra
                    .insert("committed_events".into(), sidecar.into_any().unbind());
            }
            j.stats.extra["committed_events"].clone_ref(py)
        };
        sidecar
            .bind(py)
            .get_item("events")?
            .cast::<PyList>()?
            .append(envelope)?;
        self.event_manager(py)?.call_method1("retired", (event,))?;
        Ok(())
    }
}
