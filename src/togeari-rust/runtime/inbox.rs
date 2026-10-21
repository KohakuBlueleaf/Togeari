//! The dispatcher's inbox: events from any thread, taken by the dispatcher in batches; a wait
//! for the first event releases the GIL. Its Python methods (`queue.SimpleQueue`'s) are in
//! `python.rs`.

use std::collections::VecDeque;
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use pyo3::prelude::*;

use super::state::Obj;

#[pyclass(name = "Inbox", module = "togeari._rs", frozen)]
pub struct Inbox {
    pub q: Mutex<VecDeque<Obj>>,
    pub cv: Condvar,
}

/// A wait's end, or None to wait for ever; a timeout too large for the clock is an
/// OverflowError, as `queue.SimpleQueue.get` gives.
pub fn deadline(timeout: Option<f64>) -> PyResult<Option<Instant>> {
    let Some(t) = timeout else { return Ok(None) };
    let too_large = || pyo3::exceptions::PyOverflowError::new_err("timeout value is too large");
    let d = Duration::try_from_secs_f64(t.max(0.0)).map_err(|_| too_large())?;
    Instant::now()
        .checked_add(d)
        .map(Some)
        .ok_or_else(too_large)
}

impl Inbox {
    pub fn new() -> Self {
        Inbox { q: Mutex::new(VecDeque::new()), cv: Condvar::new() }
    }

    /// Every queued event, oldest first; the inbox is left empty.
    pub fn take_all(&self) -> VecDeque<Obj> {
        std::mem::take(&mut *self.q.lock().expect("the inbox lock"))
    }

    /// Wait (the GIL released) until an event is queued or ``timeout`` passes; the first event.
    pub fn wait(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Option<Obj>> {
        let end = deadline(timeout)?;
        Ok(py.detach(|| {
            let mut q = self.q.lock().expect("the inbox lock");
            loop {
                if let Some(ev) = q.pop_front() {
                    return Some(ev);
                }
                match end {
                    None => q = self.cv.wait(q).expect("the inbox lock"),
                    Some(end) => {
                        let now = Instant::now();
                        if now >= end {
                            return None;
                        }
                        q = self
                            .cv
                            .wait_timeout(q, end - now)
                            .expect("the inbox lock")
                            .0;
                    }
                }
            }
        }))
    }

    pub fn put(&self, item: Obj) {
        self.q.lock().expect("the inbox lock").push_back(item);
        self.cv.notify_one();
    }

    pub fn is_empty(&self) -> bool {
        self.q.lock().expect("the inbox lock").is_empty()
    }
}

impl Default for Inbox {
    fn default() -> Self {
        Inbox::new()
    }
}
