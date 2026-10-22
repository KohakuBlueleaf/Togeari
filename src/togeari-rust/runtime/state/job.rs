//! Jobs: one thread of a program each, with its strands, window, counters and record.

use std::cell::RefCell;
use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::rc::Rc;

use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString};

use crate::program::core::NameId;

use super::frame::CollectorRef;
use super::inflight::{InfRef, InfState};
use super::key::Key;
use super::names::Names;
use super::strand::{StrandRef, StrandState};
use super::window::Window;
use super::{next_seq, Obj};

pub struct Param {
    pub name: NameId,
    pub py_name: Py<PyString>,
    pub has_default: bool,
    pub default: Obj,
}

/// A named address where jobs start, with its input and output fields.
pub struct Entry {
    pub name: String,
    pub py_name: Py<PyString>,
    pub addr: i64,
    pub params: Vec<Param>,
    pub outputs: Option<Vec<(Py<PyString>, NameId)>>,
    pub region_hash: Py<PyString>,
    pub py: Obj,
}

/// The job's counters; `extra` holds the keys set only sometimes, in the order first set.
#[derive(Default)]
pub struct JobStats {
    pub calls: i64,
    pub squashed: i64,
    pub retired: i64,
    pub window_peak: i64,
    pub strands_peak: i64,
    pub inflight_peak: i64,
    pub mutates_copies: i64,
    pub parks: i64,
    pub reused: i64,
    pub bytes_produced: i64,
    pub bytes_peak: i64,
    pub program: Option<Py<PyString>>,
    pub extra: IndexMap<String, Obj>,
}

/// Results kept from squashed statements: `(inputs digest, result, bytes)` by address in
/// insertion order, with their count and bytes.
#[derive(Default)]
pub struct Memo {
    pub entries: IndexMap<i64, Vec<(Py<PyBytes>, Obj, i64)>>,
    pub n: i64,
    pub bytes: i64,
}

impl Memo {
    pub fn contains(&self, addr: i64) -> bool {
        self.entries.contains_key(&addr)
    }

    pub fn add(&mut self, addr: i64, entry: (Py<PyBytes>, Obj, i64)) {
        self.bytes += entry.2;
        self.n += 1;
        self.entries.entry(addr).or_default().push(entry);
    }

    /// Drop the oldest address's oldest entry; its bytes.
    pub fn pop_oldest(&mut self) -> i64 {
        let addr = *self.entries.keys().next().expect("a memo entry");
        self.remove(addr, 0).2
    }

    /// Remove the entry of ``addr`` with inputs digest ``dig``: (result, bytes), or None.
    pub fn take(&mut self, py: Python<'_>, addr: i64, dig: &[u8]) -> Option<(Obj, i64)> {
        let i = self
            .entries
            .get(&addr)?
            .iter()
            .position(|(d, _, _)| d.as_bytes(py) == dig)?;
        let (_, value, size) = self.remove(addr, i);
        Some((value, size))
    }

    fn remove(&mut self, addr: i64, i: usize) -> (Py<PyBytes>, Obj, i64) {
        let list = self.entries.get_mut(&addr).expect("a memo address");
        let e = list.remove(i);
        if list.is_empty() {
            self.entries.shift_remove(&addr);
        }
        self.n -= 1;
        self.bytes -= e.2;
        e
    }
}

pub type HeapEntry<T> = Reverse<(Key, u64, HeapItem<T>)>;

/// A heap payload compared by nothing (the key and sequence decide).
pub struct HeapItem<T>(pub T);

impl<T> PartialEq for HeapItem<T> {
    fn eq(&self, _: &Self) -> bool {
        true
    }
}
impl<T> Eq for HeapItem<T> {}
impl<T> PartialOrd for HeapItem<T> {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl<T> Ord for HeapItem<T> {
    fn cmp(&self, _: &Self) -> std::cmp::Ordering {
        std::cmp::Ordering::Equal
    }
}

pub struct Job {
    pub id: u64,
    pub key: Obj,
    pub entry: Rc<Entry>,
    // The job's inputs: the mapping it was given
    pub inputs: Obj,

    // Live strands by id, a heap of (next key, seq, strand) with stale entries, runnable ones
    pub strands: IndexMap<u64, StrandRef>,
    pub sheap: BinaryHeap<HeapEntry<StrandRef>>,
    pub runnable: Vec<StrandRef>,

    // Window by order key, and a heap of (key, seq, inflight) for unfinished or faulted ones
    pub window: Window,
    pub wheap: BinaryHeap<HeapEntry<InfRef>>,
    pub unfinished: i64,
    pub issued: i64,

    // The outcome, the counters and the record
    pub final_names: Option<Names>,
    pub final_key: Option<Key>,
    pub failure: Option<Py<PyDict>>,
    pub stats: JobStats,
    pub record: Option<Py<PyList>>,

    // Lifecycle, pause and the handle that waits on it
    pub ended: bool,
    pub start_time: f64,
    pub paused: bool,
    pub held: Vec<InfRef>,
    pub handle: Option<Obj>,

    // Steps and held bytes, sampling, snapshots
    pub steps: i64,
    pub bytes: i64,
    pub sampled: bool,
    pub auto_key: bool,
    pub snap_at: i64,
    pub snap_off: bool,
    pub inputs_dig: Option<Py<PyBytes>>,
    pub memo: Option<Memo>,
    pub collectors: Vec<CollectorRef>,
}

pub type JobRef = Rc<RefCell<Job>>;

impl Job {
    pub fn new(id: u64, key: Obj, entry: Rc<Entry>, inputs: Obj) -> JobRef {
        super::born(super::JOBS);
        Rc::new(RefCell::new(Job {
            id,
            key,
            entry,
            inputs,
            strands: IndexMap::new(),
            sheap: BinaryHeap::new(),
            runnable: Vec::new(),
            window: Window::default(),
            wheap: BinaryHeap::new(),
            unfinished: 0,
            issued: 0,
            final_names: None,
            final_key: None,
            failure: None,
            stats: JobStats::default(),
            record: None,
            ended: false,
            start_time: 0.0,
            paused: false,
            held: Vec::new(),
            handle: None,
            steps: 0,
            bytes: 0,
            sampled: false,
            auto_key: false,
            snap_at: 0,
            snap_off: false,
            inputs_dig: None,
            memo: None,
            collectors: Vec::new(),
        }))
    }

    // ------------------------------------------------------------ strands
    pub fn add_strand(&mut self, st: &StrandRef) {
        let (id, key, runnable) = {
            let s = st.borrow();
            (s.id, s.next_key(), s.state == StrandState::Runnable)
        };
        self.strands.insert(id, st.clone());
        self.sheap
            .push(Reverse((key, next_seq(), HeapItem(st.clone()))));
        if self.strands.len() as i64 > self.stats.strands_peak {
            self.stats.strands_peak = self.strands.len() as i64;
        }
        if runnable {
            self.mark_runnable(st);
        }
    }

    /// End a strand: it lets go of its names, frames and spawner state, so the slots it waited
    /// on (whose waiter lists still name it) free.
    pub fn end_strand(&mut self, st: &StrandRef) {
        let (id, names, frames, spawn) = {
            let mut s = st.borrow_mut();
            s.state = StrandState::Ended;
            (
                s.id,
                s.names.take(),
                std::mem::replace(&mut s.frames, super::frame::no_frames()),
                s.spawn.take(),
            )
        };
        self.strands.shift_remove(&id);
        drop((names, frames, spawn));
    }

    pub fn mark_runnable(&mut self, st: &StrandRef) {
        let mut s = st.borrow_mut();
        if !s.queued {
            s.queued = true;
            drop(s);
            self.runnable.push(st.clone());
        }
    }

    pub fn take_runnable(&mut self) -> Vec<StrandRef> {
        let got = std::mem::take(&mut self.runnable);
        for st in &got {
            st.borrow_mut().queued = false;
        }
        got
    }

    pub fn oldest_live_key(&mut self) -> Option<Key> {
        loop {
            let (k, st) = {
                let top = self.sheap.peek()?;
                (top.0 .0.clone(), top.0 .2 .0.clone())
            };
            let (ended, cur) = {
                let s = st.borrow();
                (s.state == StrandState::Ended, s.next_key())
            };
            if ended {
                self.sheap.pop();
                continue;
            }
            if cur != k {
                self.sheap.pop();
                self.sheap.push(Reverse((cur, next_seq(), HeapItem(st))));
                continue;
            }
            return Some(k);
        }
    }

    // ------------------------------------------------------------ window
    pub fn add(&mut self, inf: &InfRef) {
        let key = inf.borrow().key.clone();
        self.window.add(inf.clone());
        self.wheap
            .push(Reverse((key, next_seq(), HeapItem(inf.clone()))));
        if self.window.len() as i64 > self.stats.window_peak {
            self.stats.window_peak = self.window.len() as i64;
        }
    }

    pub fn issue(&mut self, inf: &InfRef) {
        inf.borrow_mut().state = InfState::Issued;
        self.issued += 1;
        if self.issued > self.stats.inflight_peak {
            self.stats.inflight_peak = self.issued;
        }
    }

    /// The key of the oldest inflight still unfinished, or done with a fault not yet taken.
    pub fn oldest_open_key(&mut self) -> Option<Key> {
        loop {
            let x = self.sheap_top_inf()?;
            let open = {
                let b = x.borrow();
                b.state.live() || (b.state == InfState::Done && b.fault.is_some())
            };
            if open {
                return Some(x.borrow().key.clone());
            }
            self.wheap.pop();
        }
    }

    fn sheap_top_inf(&self) -> Option<InfRef> {
        self.wheap.peek().map(|e| e.0 .2 .0.clone())
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        super::died(super::JOBS);
    }
}

impl JobStats {
    /// A copy (Python objects shared), to build views from with no borrow held.
    pub fn snapshot(&self, py: Python<'_>) -> JobStats {
        JobStats {
            calls: self.calls,
            squashed: self.squashed,
            retired: self.retired,
            window_peak: self.window_peak,
            strands_peak: self.strands_peak,
            inflight_peak: self.inflight_peak,
            mutates_copies: self.mutates_copies,
            parks: self.parks,
            reused: self.reused,
            bytes_produced: self.bytes_produced,
            bytes_peak: self.bytes_peak,
            program: self.program.as_ref().map(|p| p.clone_ref(py)),
            extra: self
                .extra
                .iter()
                .map(|(k, v)| (k.clone(), v.clone_ref(py)))
                .collect(),
        }
    }

    /// The counters as a dict in the Python core's key order, then the extra keys.
    pub fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        d.set_item("calls", self.calls)?;
        d.set_item("squashed", self.squashed)?;
        d.set_item("retired", self.retired)?;
        d.set_item("window_peak", self.window_peak)?;
        d.set_item("strands_peak", self.strands_peak)?;
        d.set_item("inflight_peak", self.inflight_peak)?;
        d.set_item("mutates_copies", self.mutates_copies)?;
        d.set_item("parks", self.parks)?;
        d.set_item("reused", self.reused)?;
        d.set_item("bytes_produced", self.bytes_produced)?;
        d.set_item("bytes_peak", self.bytes_peak)?;
        if let Some(p) = &self.program {
            d.set_item("program", p.bind(py))?;
        }
        for (k, v) in &self.extra {
            d.set_item(k, v.bind(py))?;
        }
        Ok(d)
    }

    /// Python's `stats.update(d)`: known counters set in place, other keys added in order.
    pub fn update(&mut self, d: &Bound<'_, PyDict>) -> PyResult<()> {
        for (k, v) in d.iter() {
            let k: String = k.extract()?;
            let slot = match k.as_str() {
                "calls" => &mut self.calls,
                "squashed" => &mut self.squashed,
                "retired" => &mut self.retired,
                "window_peak" => &mut self.window_peak,
                "strands_peak" => &mut self.strands_peak,
                "inflight_peak" => &mut self.inflight_peak,
                "mutates_copies" => &mut self.mutates_copies,
                "parks" => &mut self.parks,
                "reused" => &mut self.reused,
                "bytes_produced" => &mut self.bytes_produced,
                "bytes_peak" => &mut self.bytes_peak,
                "program" => {
                    self.program = Some(v.str()?.unbind());
                    continue;
                }
                _ => {
                    self.extra.insert(k, v.unbind());
                    continue;
                }
            };
            *slot = v.extract()?;
        }
        Ok(())
    }

    pub fn get_int(&self, py: Python<'_>, key: &str) -> i64 {
        self.extra
            .get(key)
            .and_then(|v| v.extract::<i64>(py).ok())
            .unwrap_or(0)
    }
}
