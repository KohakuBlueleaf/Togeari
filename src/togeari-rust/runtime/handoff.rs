//! Private causal GIL-handoff probe for the native integration test.
use std::sync::{Condvar, Mutex};
use std::time::Duration;

use pyo3::prelude::*;

#[derive(Default)]
struct State {
    ready: bool,
    entered: bool,
    acknowledged: bool,
    closed: bool,
}

#[pyclass(name = "_GilHandoffProbe", module = "togeari._rs", frozen)]
#[derive(Default)]
pub struct GilHandoffProbe {
    state: Mutex<State>,
    changed: Condvar,
}

pub fn duration(seconds: f64) -> PyResult<Duration> {
    if !seconds.is_finite() || !(0.0..=60.0).contains(&seconds) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "probe timeout must be between zero and 60 seconds",
        ));
    }
    Duration::try_from_secs_f64(seconds)
        .map_err(|_| pyo3::exceptions::PyValueError::new_err("invalid probe timeout"))
}

impl GilHandoffProbe {
    /// Called only inside the real Core::region closure. No Python callbacks or GIL API.
    pub fn region(&self, timeout: Duration) -> bool {
        let mut state = self.state.lock().expect("probe lock");
        if !state.ready || state.closed || state.entered {
            return false;
        }
        state.entered = true;
        self.changed.notify_all();
        let (mut state, _) = self
            .changed
            .wait_timeout_while(state, timeout, |s| !s.acknowledged && !s.closed)
            .expect("probe lock");
        let acknowledged = state.acknowledged;
        state.closed = true;
        self.changed.notify_all();
        acknowledged
    }
}

#[pymethods]
impl GilHandoffProbe {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    #[getter]
    fn ready(&self) -> bool {
        self.state.lock().expect("probe lock").ready
    }

    fn wait_entered(&self, py: Python<'_>, timeout_s: f64) -> PyResult<bool> {
        let timeout = duration(timeout_s)?;
        Ok(py.detach(|| {
            let mut state = self.state.lock().expect("probe lock");
            if state.ready || state.closed {
                return false;
            }
            state.ready = true;
            self.changed.notify_all();
            let (mut state, _) = self
                .changed
                .wait_timeout_while(state, timeout, |s| !s.entered && !s.closed)
                .expect("probe lock");
            if !state.entered {
                state.closed = true;
            }
            // Guard drops inside detach, BEFORE this thread reacquires the GIL.
            state.entered && !state.closed
        }))
    }

    /// Calling this from Python proves the neighbour reacquired the GIL.
    fn acknowledge(&self) -> bool {
        let mut state = self.state.lock().expect("probe lock");
        if !state.entered || state.closed {
            return false;
        }
        state.acknowledged = true;
        self.changed.notify_all();
        true
    }

    fn close(&self) {
        self.state.lock().expect("probe lock").closed = true;
        self.changed.notify_all();
    }
}
