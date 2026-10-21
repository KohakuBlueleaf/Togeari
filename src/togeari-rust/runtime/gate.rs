//! The region gate: set while the dispatcher runs a GIL-free region. Another thread that reads
//! the core or a native lane waits outside the region, the GIL released, until it ends.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Condvar, Mutex};

use pyo3::prelude::*;

#[derive(Default)]
pub struct Gate {
    flag: AtomicBool,
    lock: Mutex<()>,
    cv: Condvar,
}

impl Gate {
    /// Set before the GIL is released for a region.
    pub fn enter(&self) {
        self.flag.store(true, Ordering::Release);
    }

    /// Cleared once the region's thread holds the GIL again; waiters wake.
    pub fn leave(&self) {
        self.flag.store(false, Ordering::Release);
        let _g = self.lock.lock().expect("the gate lock");
        self.cv.notify_all();
    }

    pub fn active(&self) -> bool {
        self.flag.load(Ordering::Acquire)
    }

    /// Return once no region runs; waits with the GIL released.
    pub fn wait(&self, py: Python<'_>) {
        if !self.active() {
            return;
        }
        py.detach(|| {
            let mut g = self.lock.lock().expect("the gate lock");
            while self.flag.load(Ordering::Acquire) {
                g = self.cv.wait(g).expect("the gate lock");
            }
        });
    }
}
