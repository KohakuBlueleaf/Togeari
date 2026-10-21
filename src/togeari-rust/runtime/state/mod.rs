//! Job state. Objects hold each other as the Python core's do (`Weak` back to a job), so a
//! slot is freed where the Python core frees it.

use std::cell::Cell;
use std::sync::atomic::{AtomicI64, Ordering};

use pyo3::prelude::*;

pub mod frame;
pub mod inflight;
pub mod job;
pub mod key;
pub mod names;
pub mod slot;
pub mod strand;
pub mod window;

pub use frame::{EachSpawn, Frame, FrameKind, Join, JoinRef};
pub use inflight::{Fault, InfRef, InfState, Inflight, Snap, Version};
pub use job::{Entry, Job, JobRef, Memo, Param};
pub use key::Key;
pub use names::{NameMap, Names};
pub use slot::{deref, effective, Freed, Slot, SlotRef, SlotState, Waiter};
pub use strand::{Resume, Strand, StrandRef, StrandState};

pub type Obj = Py<PyAny>;

/// A value as slots share it: copying a slot's value never touches the object's refcount.
pub type Val = std::rc::Rc<Obj>;

thread_local! {
    static IDS: Cell<u64> = const { Cell::new(1) };
    static SEQ: Cell<u64> = const { Cell::new(0) };
}

/// The next object id (jobs, strands, inflights, slots).
pub fn next_id() -> u64 {
    IDS.with(|c| {
        let v = c.get();
        c.set(v + 1);
        v
    })
}

/// The next heap sequence number (ties between equal keys go to the older entry).
pub fn next_seq() -> u64 {
    SEQ.with(|c| {
        let v = c.get();
        c.set(v + 1);
        v
    })
}

/// Live jobs, strands, inflights and slots of every core in the process.
pub static LIVE: [AtomicI64; 4] = [const { AtomicI64::new(0) }; 4];
pub const JOBS: usize = 0;
pub const STRANDS: usize = 1;
pub const INFLIGHTS: usize = 2;
pub const SLOTS: usize = 3;

pub fn born(kind: usize) {
    LIVE[kind].fetch_add(1, Ordering::Relaxed);
}

pub fn died(kind: usize) {
    LIVE[kind].fetch_sub(1, Ordering::Relaxed);
}

pub fn live() -> [(&'static str, i64); 4] {
    let n = |k: usize| LIVE[k].load(Ordering::Relaxed);
    [
        ("jobs", n(JOBS)),
        ("strands", n(STRANDS)),
        ("inflights", n(INFLIGHTS)),
        ("slots", n(SLOTS)),
    ]
}
