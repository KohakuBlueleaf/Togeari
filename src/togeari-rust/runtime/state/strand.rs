//! Strands: one program counter each, walking code memory in program order.

use std::cell::RefCell;
use std::rc::{Rc, Weak};

use super::frame::{EachSpawn, Frames};
use super::job::Job;
use super::key::{Key, Prefix};
use super::names::NameMap;
use super::next_id;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StrandState {
    Runnable,
    Parked,
    Stalled,
    Ended,
}

impl StrandState {
    pub fn name(self) -> &'static str {
        match self {
            StrandState::Runnable => "runnable",
            StrandState::Parked => "parked",
            StrandState::Stalled => "stalled",
            StrandState::Ended => "ended",
        }
    }
}

/// Why a strand re-runs its control instruction when woken.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Resume {
    No,
    Parked,
    Stalled,
}

pub struct Strand {
    pub id: u64,
    pub job_id: u64,
    pub job: Weak<RefCell<Job>>,
    pub pc: i64,
    pub prefix: Prefix,
    pub counter: i64,
    pub frames: Frames,
    pub names: Option<NameMap>,
    pub state: StrandState,
    pub resume: Resume,
    // In the job's runnable list
    pub queued: bool,
    // Lazy `each` iterations this strand starts (None for an ordinary strand)
    pub spawn: Option<Rc<RefCell<EachSpawn>>>,
}

pub type StrandRef = Rc<RefCell<Strand>>;

impl Strand {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        job: &Rc<RefCell<Job>>,
        job_id: u64,
        pc: i64,
        prefix: Prefix,
        counter: i64,
        frames: Frames,
        names: Option<NameMap>,
        spawn: Option<Rc<RefCell<EachSpawn>>>,
    ) -> StrandRef {
        super::born(super::STRANDS);
        Rc::new(RefCell::new(Strand {
            id: next_id(),
            job_id,
            job: Rc::downgrade(job),
            pc,
            prefix,
            counter,
            frames,
            names,
            state: StrandState::Runnable,
            resume: Resume::No,
            queued: false,
            spawn,
        }))
    }

    pub fn next_key(&self) -> Key {
        Key::of(&self.prefix, self.counter)
    }

    /// Take the next order key.
    pub fn consume(&mut self) -> Key {
        let k = Key::of(&self.prefix, self.counter);
        self.counter += 1;
        k
    }
}

impl Drop for Strand {
    fn drop(&mut self) {
        super::died(super::STRANDS);
    }
}
