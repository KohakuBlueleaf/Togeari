//! Regions a strand is inside (frames), the joins of parked regions, and the lazy `each`
//! spawner's state.

use std::cell::RefCell;
use std::rc::Rc;

use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::PyList;

use crate::program::core::{Instr, NameId, Table};

use super::inflight::Snap;
use super::key::{Key, Prefix};
use super::names::Names;
use super::slot::{SlotRef, SlotState};
use super::Val;

/// The phis a parked region (branch arm, `each` iteration) resolves where it reconverges.
pub struct Join {
    pub phis: IndexMap<NameId, SlotRef>,
    pub table: Option<Rc<Table>>,
    pub resolved: bool,
    pub all_names: bool,
}

pub type JoinRef = Rc<RefCell<Join>>;

impl Join {
    pub fn new(
        phis: IndexMap<NameId, SlotRef>,
        table: Option<Rc<Table>>,
        all_names: bool,
    ) -> JoinRef {
        Rc::new(RefCell::new(Join { phis, table, resolved: false, all_names }))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FrameKind {
    Branch,
    Iter,
}

impl FrameKind {
    pub fn name(self) -> &'static str {
        match self {
            FrameKind::Branch => "branch",
            FrameKind::Iter => "iter",
        }
    }
}

/// Private accumulation owned by one dynamic collection invocation.
pub struct Collector {
    pub ins: Rc<Instr>,
    pub key: Key,
    pub count: usize,
    pub output: SlotRef,
    pub values: Vec<Val>,
    pub slots: Vec<SlotRef>,
    pub snapshot: Option<Snap>,
    pub active: bool,
    pub queued: bool,
}

pub type CollectorRef = Rc<RefCell<Collector>>;

impl Collector {
    pub fn abort(&mut self) {
        self.active = false;
        self.output.borrow_mut().state = SlotState::Squashed;
        self.values.clear();
        self.slots.clear();
        self.snapshot = None;
    }
}

/// A region a strand is inside: where it rejoins, the key to restore, its join if parked.
/// Frames never change once pushed; a strand's frames are shared.
pub struct Frame {
    pub reconv: Option<i64>,
    pub saved: Option<(Prefix, i64)>,
    pub join: Option<JoinRef>,
    pub branch: Option<i64>,
    pub kind: FrameKind,
    pub each: Option<i64>,
    pub items: Option<Rc<Py<PyList>>>,
    pub i: i64,
    pub base: Option<Key>,
    pub collector: Option<CollectorRef>,
}

impl Frame {
    pub fn branch(
        reconv: Option<i64>,
        saved: (Prefix, i64),
        join: Option<JoinRef>,
        at: i64,
    ) -> Self {
        Frame {
            reconv,
            saved: Some(saved),
            join,
            branch: Some(at),
            kind: FrameKind::Branch,
            each: None,
            items: None,
            i: 0,
            base: None,
            collector: None,
        }
    }

    pub fn copy(&self) -> Self {
        Frame {
            reconv: self.reconv,
            saved: self.saved.clone(),
            join: self.join.clone(),
            branch: self.branch,
            kind: self.kind,
            each: self.each,
            items: self.items.clone(),
            i: self.i,
            base: self.base.clone(),
            collector: self.collector.clone(),
        }
    }
}

pub type Frames = Rc<Vec<Rc<Frame>>>;

pub fn no_frames() -> Frames {
    Rc::new(Vec::new())
}

/// `(*frames, f)`.
pub fn pushed(frames: &Frames, f: Frame) -> Frames {
    let mut v = (**frames).clone();
    v.push(Rc::new(f));
    Rc::new(v)
}

/// `frames[:n]`.
pub fn cut(frames: &Frames, n: usize) -> Frames {
    Rc::new(frames[..n].to_vec())
}

/// The iterations of one `each` still to start, and what each new one reads: the items and
/// their indexes as shared values.
pub struct EachSpawn {
    pub ins: i64,
    pub items: Rc<Py<PyList>>,
    pub vals: Vec<Val>,
    pub idx: Vec<Val>,
    pub n_items: i64,
    pub key: Key,
    pub base: Names,
    pub saved: (Prefix, i64),
    pub frames: Frames,
    // Names each iteration's join carries to the next, sorted
    pub carried: Vec<NameId>,
    pub table: Option<Rc<Table>>,
    // The last iteration's join (the names after the loop read its phis)
    pub last: JoinRef,
    pub prev: Option<JoinRef>,
    pub collector: Option<CollectorRef>,
}
