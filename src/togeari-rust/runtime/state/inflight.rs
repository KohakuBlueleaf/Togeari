//! Inflights: one fetched statement each, from fetch to retire.

use std::cell::RefCell;
use std::rc::{Rc, Weak};

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::program::core::Instr;
use crate::runtime::python::Owner;

use super::frame::{CollectorRef, Frames};
use super::job::Job;
use super::key::{Key, Prefix};
use super::names::Names;
use super::slot::SlotRef;
use super::{next_id, Obj};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InfState {
    Waiting,
    Ready,
    Issued,
    Done,
    Retired,
    Squashed,
}

impl InfState {
    pub fn name(self) -> &'static str {
        match self {
            InfState::Waiting => "waiting",
            InfState::Ready => "ready",
            InfState::Issued => "issued",
            InfState::Done => "done",
            InfState::Retired => "retired",
            InfState::Squashed => "squashed",
        }
    }

    /// Waiting, ready or issued.
    pub fn live(self) -> bool {
        matches!(self, InfState::Waiting | InfState::Ready | InfState::Issued)
    }
}

/// A marked fault: its error (`kind`, `node`, `message`) and whether a handler may take it.
pub struct Fault {
    pub error: Py<PyDict>,
    pub catchable: bool,
}

/// A strand's view just before a statement: names, frames, prefix, counter.
#[derive(Clone)]
pub struct Snap {
    pub names: Names,
    pub frames: Frames,
    pub prefix: Prefix,
    pub counter: i64,
}

/// A `mutates` port (its index among the call's `mutates`): the slot before and the version
/// slot after the change.
pub struct Version {
    pub k: usize,
    pub before: SlotRef,
    pub version: SlotRef,
}

pub struct Inflight {
    pub id: u64,
    pub job_id: u64,
    pub job: Weak<RefCell<Job>>,
    pub ins: Rc<Instr>,
    pub key: Key,
    /// Logical operation identity is assigned at fetch, before any input/record decisions.
    pub invocation_id: Option<String>,
    pub attempt_ids: Vec<String>,
    pub resolution: &'static str,

    // Input slots (in the call's argument order) and output slots, `mutates` versions
    pub inputs: Vec<SlotRef>,
    pub outs: Vec<Option<SlotRef>>,
    pub versions: Option<Vec<Version>>,

    // State, the work item and the values it ran with
    pub state: InfState,
    pub fault: Option<Fault>,
    pub snapshot: Option<Snap>,
    pub item: Option<Obj>,
    pub owner: Option<Py<Owner>>,
    pub copy: bool,
    pub values: Option<Py<PyDict>>,
    pub result: Option<Obj>,
    pub kept: Option<Obj>,
    pub mutated: Option<Obj>,

    // Timing, store and squash reuse
    pub t_issue: f64,
    pub t_done: f64,
    pub store_key: Option<Obj>,
    pub hit: bool,
    pub control: bool,
    pub dig: Option<Obj>,
    pub reused: bool,
    pub collector: Option<CollectorRef>,
    pub collect_index: Option<usize>,
}

pub type InfRef = Rc<RefCell<Inflight>>;

impl Inflight {
    pub fn new(job: &Rc<RefCell<Job>>, job_id: u64, ins: Rc<Instr>, key: Key) -> InfRef {
        super::born(super::INFLIGHTS);
        Rc::new(RefCell::new(Inflight {
            id: next_id(),
            job_id,
            job: Rc::downgrade(job),
            ins,
            key,
            invocation_id: None,
            attempt_ids: Vec::new(),
            resolution: "not_submitted",
            inputs: Vec::new(),
            outs: Vec::new(),
            versions: None,
            state: InfState::Waiting,
            fault: None,
            snapshot: None,
            item: None,
            owner: None,
            copy: false,
            values: None,
            result: None,
            kept: None,
            mutated: None,
            t_issue: 0.0,
            t_done: 0.0,
            store_key: None,
            hit: false,
            control: false,
            dig: None,
            reused: false,
            collector: None,
            collect_index: None,
        }))
    }

    /// Let go of values, work item, view and slots (retired or squashed).
    pub fn release(&mut self) {
        self.collector = None;
        self.values = None;
        self.result = None;
        self.snapshot = None;
        self.kept = None;
        self.mutated = None;
        self.versions = None;
        self.item = None;
        self.inputs = Vec::new();
        self.outs = Vec::new();
    }
}

impl Drop for Inflight {
    fn drop(&mut self) {
        super::died(super::INFLIGHTS);
    }
}
