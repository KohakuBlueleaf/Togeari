//! Name maps: a shared base overlaid by a small delta, folded into a new base when large;
//! `Names` is an immutable view of one.

use std::rc::Rc;

use indexmap::IndexMap;

use crate::program::core::NameId;

use super::slot::SlotRef;

pub type Dict = Rc<IndexMap<NameId, SlotRef>>;

/// Deltas larger than this fold into a new base.
pub const FOLD: usize = 32;

fn merged(base: &Dict, delta: &Dict) -> IndexMap<NameId, SlotRef> {
    let mut out = (**base).clone();
    for (k, v) in delta.iter() {
        out.insert(*k, v.clone());
    }
    out
}

#[derive(Clone)]
pub struct Names {
    pub base: Dict,
    pub delta: Dict,
}

impl Names {
    pub fn get(&self, name: NameId) -> Option<SlotRef> {
        self.delta
            .get(&name)
            .or_else(|| self.base.get(&name))
            .cloned()
    }

    /// Every name and slot, base order first (Python's `{**base, **delta}`).
    pub fn to_dict(&self) -> IndexMap<NameId, SlotRef> {
        if self.delta.is_empty() {
            (*self.base).clone()
        } else {
            merged(&self.base, &self.delta)
        }
    }

    pub fn keys(&self) -> Vec<NameId> {
        self.to_dict().keys().copied().collect()
    }
}

pub struct NameMap {
    pub base: Dict,
    pub delta: Dict,
}

impl Default for NameMap {
    fn default() -> Self {
        NameMap { base: Rc::new(IndexMap::new()), delta: Rc::new(IndexMap::new()) }
    }
}

impl NameMap {
    pub fn with_base(base: IndexMap<NameId, SlotRef>) -> Self {
        NameMap { base: Rc::new(base), delta: Rc::new(IndexMap::new()) }
    }

    /// A map starting from ``view``, with ``extra`` names written over it.
    pub fn of(view: &Names, extra: Option<IndexMap<NameId, SlotRef>>) -> Self {
        match extra {
            None => NameMap { base: view.base.clone(), delta: view.delta.clone() },
            Some(extra) if extra.is_empty() => {
                NameMap { base: view.base.clone(), delta: view.delta.clone() }
            }
            Some(extra) => {
                let mut d = (*view.delta).clone();
                for (k, v) in extra {
                    d.insert(k, v);
                }
                let mut nm = NameMap { base: view.base.clone(), delta: Rc::new(d) };
                if nm.delta.len() > FOLD {
                    nm.fold();
                }
                nm
            }
        }
    }

    pub fn get(&self, name: NameId) -> Option<SlotRef> {
        self.delta
            .get(&name)
            .or_else(|| self.base.get(&name))
            .cloned()
    }

    pub fn set(&mut self, name: NameId, slot: SlotRef) {
        Rc::make_mut(&mut self.delta).insert(name, slot);
        if self.delta.len() > FOLD {
            self.fold();
        }
    }

    pub fn fold(&mut self) {
        self.base = Rc::new(merged(&self.base, &self.delta));
        self.delta = Rc::new(IndexMap::new());
    }

    pub fn snapshot(&self) -> Names {
        Names { base: self.base.clone(), delta: self.delta.clone() }
    }

    pub fn to_dict(&self) -> IndexMap<NameId, SlotRef> {
        merged(&self.base, &self.delta)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime::state::slot::Slot;

    #[test]
    fn a_map_overlays_and_folds_in_python_order() {
        let mut m = NameMap::default();
        let a = Slot::pending();
        let b = Slot::pending();
        m.set(2, a.clone());
        m.set(1, b.clone());
        let snap = m.snapshot();
        m.set(2, b.clone());
        assert!(Rc::ptr_eq(&snap.get(2).unwrap(), &a));
        assert!(Rc::ptr_eq(&m.get(2).unwrap(), &b));
        for i in 10..(10 + FOLD as u32 - 1) {
            m.set(i, Slot::pending());
        }
        assert!(m.delta.is_empty());
        assert_eq!(m.to_dict().keys().take(2).copied().collect::<Vec<_>>(), vec![2, 1]);
    }
}
