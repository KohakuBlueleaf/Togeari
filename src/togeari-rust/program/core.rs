//! Code memory as the core reads it: instructions by address, their decode tables, interned
//! names, regions and `@try` tables. Imported once per region from the Python code memory.

use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

pub type NameId = u32;
pub type Obj = Py<PyAny>;

/// Names as small integers, with their text.
#[derive(Default)]
pub struct Interner {
    ids: HashMap<Rc<str>, NameId>,
    strs: Vec<Rc<str>>,
}

impl Interner {
    pub fn id(&mut self, s: &str) -> NameId {
        if let Some(&id) = self.ids.get(s) {
            return id;
        }
        let id = self.strs.len() as NameId;
        let rc: Rc<str> = Rc::from(s);
        self.strs.push(rc.clone());
        self.ids.insert(rc, id);
        id
    }

    pub fn text(&self, id: NameId) -> &str {
        &self.strs[id as usize]
    }

    /// `ids` sorted by their text (Python's string order: code points, as UTF-8 bytes).
    pub fn sorted(&self, ids: impl IntoIterator<Item = NameId>) -> Vec<NameId> {
        let mut v: Vec<NameId> = ids.into_iter().collect();
        v.sort_by(|a, b| self.text(*a).cmp(self.text(*b)));
        v.dedup();
        v
    }
}

/// A set of names; `all` stands for the `ALL` marker (every name may change).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct NameSet {
    pub all: bool,
    ids: Vec<NameId>,
}

impl NameSet {
    pub fn new(all: bool, ids: impl IntoIterator<Item = NameId>) -> Self {
        let mut v: Vec<NameId> = ids.into_iter().collect();
        v.sort_unstable();
        v.dedup();
        NameSet { all, ids: v }
    }

    pub fn contains(&self, id: NameId) -> bool {
        self.ids.binary_search(&id).is_ok()
    }

    /// The names without the `ALL` marker.
    pub fn ids(&self) -> &[NameId] {
        &self.ids
    }
}

/// Names writable from each address of a region (the decode table of one control instruction).
pub type Table = HashMap<i64, NameSet>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Op {
    Call,
    Assign,
    Branch,
    Switch,
    Jump,
    Each,
    IterEnd,
    End,
}

impl Op {
    pub fn parse(s: &str) -> Option<Op> {
        Some(match s {
            "call" => Op::Call,
            "assign" => Op::Assign,
            "branch" => Op::Branch,
            "switch" => Op::Switch,
            "jump" => Op::Jump,
            "each" => Op::Each,
            "iter_end" => Op::IterEnd,
            "end" => Op::End,
            _ => return None,
        })
    }
}

/// An instruction operand: a name, a literal or default (copied per use when a container), or
/// a required input nobody gave.
pub enum Operand {
    Name(NameId),
    Lit { value: Rc<Obj>, container: bool },
    Missing(Py<PyString>),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StoreMode {
    No,
    Content,
    Positional,
}

/// What a call instruction is bound to.
pub struct Binding {
    pub inline: bool,
    pub func: Option<Obj>,
    pub lane: Option<Obj>,
    pub lane_name: Option<Py<PyString>>,
    pub adapter: Option<Obj>,
    pub store: StoreMode,
    pub idempotent: bool,
    pub warn_us: Option<f64>,
}

/// One decoded statement and, for a control instruction, its decode tables.
pub struct Instr {
    pub addr: i64,
    pub op: Op,
    pub py: Obj,
    pub next: Option<i64>,
    pub targets: Vec<i64>,
    pub cases: Vec<(Obj, i64)>,
    pub default: Option<i64>,
    pub hint: Py<PyDict>,
    pub hint_timeout: Option<Obj>,
    pub hint_priority: Option<Obj>,
    // ``int(hint priority)`` (0 without one) when it is an int Rust holds
    pub hint_rank: Option<i64>,

    // Calls
    pub node: Py<PyString>,
    pub args: Vec<(Py<PyString>, Operand)>,
    pub outs: Vec<Option<NameId>>,
    pub n_sig_out: usize,
    pub sig_outputs: Vec<String>,
    pub ordered: bool,
    pub mutates: Vec<Py<PyString>>,
    // Per `mutates` port: its argument's index, and its index among the signature's outputs
    pub mutates_args: Vec<Option<usize>>,
    pub mutates_out: Vec<Option<usize>>,
    pub binding: Option<Binding>,

    // Assign, branch, switch, each
    pub target: Option<NameId>,
    pub operand: Option<Operand>,
    pub operand_name: Option<Py<PyString>>,
    pub item: Option<NameId>,
    pub index: Option<NameId>,
    pub body: Option<i64>,
    pub after: Option<i64>,
    pub iter_end: Option<i64>,
    pub has_exit: bool,
    pub each_addr: Option<i64>,
    pub collect_value: Option<Operand>,
    pub collect_target: Option<NameId>,
    pub try_id: Option<(i64, i64)>,

    // Decode tables of a control instruction
    pub reconv: Option<i64>,
    pub structured: bool,
    pub may_write: NameSet,
    pub region_writes: Option<Rc<Table>>,
    pub body_may_write: NameSet,
    pub body_writes: Option<Rc<Table>>,
    pub carried: NameSet,
}

pub struct Region {
    pub start: i64,
    pub end: i64,
    pub hash: Py<PyString>,
}

pub struct TryInfo {
    pub body: HashSet<i64>,
    pub handler: i64,
    pub error_name: Option<NameId>,
}

/// Every imported region: instructions by address, regions by start, `@try` tables, names.
#[derive(Default)]
pub struct Code {
    pub names: Interner,
    pub py_names: Vec<Py<PyString>>,
    pub instrs: Vec<Option<Rc<Instr>>>,
    pub regions: Vec<Rc<Region>>,
    pub tries: HashMap<(i64, i64), Rc<TryInfo>>,
}

impl Code {
    pub fn instr(&self, addr: i64) -> Option<Rc<Instr>> {
        if addr < 0 {
            return None;
        }
        self.instrs.get(addr as usize).and_then(|x| x.clone())
    }

    pub fn region_of(&self, addr: i64) -> Option<Rc<Region>> {
        let i = self.regions.partition_point(|r| r.start <= addr);
        if i == 0 {
            return None;
        }
        let r = &self.regions[i - 1];
        (addr < r.end).then(|| r.clone())
    }

    pub fn has_region(&self, start: i64) -> bool {
        self.regions.iter().any(|r| r.start == start)
    }

    pub fn add_region(&mut self, region: Region) {
        let i = self.regions.partition_point(|r| r.start < region.start);
        self.regions.insert(i, Rc::new(region));
    }

    pub fn name_py<'py>(&self, py: Python<'py>, id: NameId) -> &Bound<'py, PyString> {
        self.py_names[id as usize].bind(py)
    }

    pub fn intern(&mut self, py: Python<'_>, s: &str) -> NameId {
        let id = self.names.id(s);
        while self.py_names.len() <= id as usize {
            let text = self.names.text(self.py_names.len() as NameId).to_string();
            self.py_names.push(PyString::new(py, &text).unbind());
        }
        id
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_intern_once_and_sort_by_text() {
        let mut n = Interner::default();
        let b = n.id("b");
        let a = n.id("a");
        assert_eq!(n.id("b"), b);
        assert_eq!(n.sorted([b, a, b]), vec![a, b]);
        assert_eq!(n.text(a), "a");
    }

    #[test]
    fn a_name_set_answers_membership() {
        let s = NameSet::new(false, [3, 1, 3]);
        assert!(s.contains(1) && s.contains(3) && !s.contains(2));
        assert_eq!(s.ids(), &[1, 3]);
    }
}
