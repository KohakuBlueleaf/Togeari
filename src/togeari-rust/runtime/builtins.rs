//! Builtin policy methods the core runs natively (spec/11 §11.4), known by the function a
//! policy's method resolves to; a method an instance sets on itself is the user's.

use std::collections::HashMap;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt};

use crate::runtime::state::Obj;

/// A builtin method body.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tag {
    // Former contract defaults, and the builtin formers
    FormerKey,
    FormerDue,
    FormerDeadline,
    FormerTake,
    GreedyDeadline,
    GreedyTake,
    SowLimit,
    SowDue,
    SowDeadline,
    SowTake,
    BucketedKey,
    BucketedDue,
    BucketedDeadline,
    BucketedTake,

    // Selector contract defaults, and the builtin selectors
    SelectOrder,
    SelectAdmits,
    LeastLoadedOrder,
    ThroughputOrder,
    RoundRobinOrder,
    FallbackOrder,
    ByHintAdmits,
    AffinityAdmits,
    AffinityPreferred,
    AffinityItemKey,

    // Issue orders and admission rules
    FifoOrder,
    FifoPriority,
    OldestOrder,
    OldestPriority,
    DeepestOrder,
    DeepestPriority,
    LaneAwareOrder,
    LaneAwarePriority,
    HintOrder,
    HintPriority,
    FixedAllows,
    TargetLoadAllows,
    MemoryBoundAllows,
    AllAllows,

    // The engine contract's `key` (always None)
    EngineKey,
}

pub struct Builtins {
    by_fn: HashMap<usize, Tag>,
    fns: Vec<Obj>,
}

fn fun(py: Python<'_>, module: &str, cls: &str, name: &str) -> PyResult<Obj> {
    Ok(py.import(module)?.getattr(cls)?.getattr(name)?.unbind())
}

impl Builtins {
    pub fn new(py: Python<'_>) -> PyResult<Builtins> {
        let c = "togeari.core.contracts.policies";
        let f = "togeari.core.policies.former";
        let s = "togeari.core.policies.select";
        let i = "togeari.core.policies.issue";
        let a = "togeari.core.policies.admission";
        let table: Vec<(&str, &str, &str, Tag)> = vec![
            (c, "Former", "key", Tag::FormerKey),
            (c, "Former", "due", Tag::FormerDue),
            (c, "Former", "deadline", Tag::FormerDeadline),
            (c, "Former", "take", Tag::FormerTake),
            (f, "Greedy", "deadline", Tag::GreedyDeadline),
            (f, "Greedy", "take", Tag::GreedyTake),
            (f, "SizeOrWait", "_limit", Tag::SowLimit),
            (f, "SizeOrWait", "due", Tag::SowDue),
            (f, "SizeOrWait", "deadline", Tag::SowDeadline),
            (f, "SizeOrWait", "take", Tag::SowTake),
            (f, "Bucketed", "key", Tag::BucketedKey),
            (f, "Bucketed", "due", Tag::BucketedDue),
            (f, "Bucketed", "deadline", Tag::BucketedDeadline),
            (f, "Bucketed", "take", Tag::BucketedTake),
            (c, "Selector", "order", Tag::SelectOrder),
            (c, "Selector", "admits", Tag::SelectAdmits),
            (s, "LeastLoaded", "order", Tag::LeastLoadedOrder),
            (s, "Throughput", "order", Tag::ThroughputOrder),
            (s, "RoundRobin", "order", Tag::RoundRobinOrder),
            (s, "Affinity", "order", Tag::FallbackOrder),
            (s, "ByHint", "order", Tag::FallbackOrder),
            (s, "ByHint", "admits", Tag::ByHintAdmits),
            (s, "Affinity", "admits", Tag::AffinityAdmits),
            (s, "Affinity", "preferred", Tag::AffinityPreferred),
            (s, "Affinity", "item_key", Tag::AffinityItemKey),
            (i, "Fifo", "order", Tag::FifoOrder),
            (i, "Fifo", "priority", Tag::FifoPriority),
            (i, "OldestJobFirst", "order", Tag::OldestOrder),
            (i, "OldestJobFirst", "priority", Tag::OldestPriority),
            (i, "DeepestFirst", "order", Tag::DeepestOrder),
            (i, "DeepestFirst", "priority", Tag::DeepestPriority),
            (i, "LaneAware", "order", Tag::LaneAwareOrder),
            (i, "LaneAware", "priority", Tag::LaneAwarePriority),
            (i, "HintPriority", "order", Tag::HintOrder),
            (i, "HintPriority", "priority", Tag::HintPriority),
            (a, "Fixed", "allows", Tag::FixedAllows),
            (a, "TargetLoad", "allows", Tag::TargetLoadAllows),
            (a, "MemoryBound", "allows", Tag::MemoryBoundAllows),
            (a, "All", "allows", Tag::AllAllows),
            ("togeari.core.contracts.engine", "EngineBase", "key", Tag::EngineKey),
        ];
        let mut by_fn = HashMap::new();
        let mut fns = Vec::new();
        for (m, cls, name, tag) in table {
            let o = fun(py, m, cls, name)?;
            by_fn.insert(o.as_ptr() as usize, tag);
            fns.push(o);
        }
        Ok(Builtins { by_fn, fns })
    }

    /// The builtin body ``obj.name`` runs, or None when it is the user's (or missing).
    pub fn method(
        &self,
        py: Python<'_>,
        obj: &Bound<'_, PyAny>,
        name: &'static str,
    ) -> PyResult<Option<Tag>> {
        if let Some(d) = obj.getattr_opt(intern!(py, "__dict__"))? {
            if let Ok(d) = d.cast::<PyDict>() {
                if !d.is_empty() && d.contains(name)? {
                    return Ok(None);
                }
            }
        }
        // The class attribute as it is now (a class patched later runs its patch)
        let tag = match obj.get_type().getattr_opt(name)? {
            Some(f) => self.by_fn.get(&(f.as_ptr() as usize)).copied(),
            None => None,
        };
        Ok(tag)
    }

    pub fn visit(&self, visit: &pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        for f in &self.fns {
            visit.call(f)?;
        }
        Ok(())
    }
}

/// A plain number as f64: an int or a float (a bool counts as its int); None otherwise.
pub fn plain_f64(v: &Bound<'_, PyAny>) -> Option<f64> {
    if v.is_exact_instance_of::<PyFloat>() || v.is_instance_of::<PyInt>() {
        v.extract::<f64>().ok()
    } else {
        None
    }
}
