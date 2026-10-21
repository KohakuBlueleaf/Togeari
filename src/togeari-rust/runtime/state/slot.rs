//! Value slots: one renamed value each; a phi stands for a write an unresolved region makes.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

use super::key::Key;
use super::{next_id, InfRef, Obj, StrandRef, Val};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SlotState {
    Pending,
    Ready,
    Squashed,
}

/// What waits on a pending slot.
#[derive(Clone)]
pub enum Waiter {
    Strand(StrandRef),
    Inf(InfRef),
}

/// The dispatcher's queue of `(job id, bytes)` released by freed held slots.
pub type Freed = Rc<RefCell<VecDeque<(u64, i64)>>>;

/// A held slot's bytes and the job they count toward.
pub struct Held {
    pub job: u64,
    pub size: i64,
    pub freed: Freed,
}

pub struct Slot {
    pub value: Option<Val>,
    pub state: SlotState,
    pub waiters: Vec<Waiter>,
    pub forward: Option<SlotRef>,
    pub phi: bool,
    // Pending in-place changes: (order key, version slot) of `mutates` calls on this value
    pub muts: Vec<(Key, SlotRef)>,
    pub id: u64,
    pub held: Option<Held>,
}

pub type SlotRef = Rc<RefCell<Slot>>;

impl Slot {
    pub fn new(value: Option<Val>, state: SlotState, phi: bool) -> SlotRef {
        super::born(super::SLOTS);
        Rc::new(RefCell::new(Slot {
            value,
            state,
            waiters: Vec::new(),
            forward: None,
            phi,
            muts: Vec::new(),
            id: next_id(),
            held: None,
        }))
    }

    pub fn pending() -> SlotRef {
        Slot::new(None, SlotState::Pending, false)
    }

    pub fn phi() -> SlotRef {
        Slot::new(None, SlotState::Pending, true)
    }

    pub fn constant(value: Obj) -> SlotRef {
        Slot::new(Some(Rc::new(value)), SlotState::Ready, false)
    }

    /// A filled slot sharing ``value``.
    pub fn shared(value: &Val) -> SlotRef {
        Slot::new(Some(value.clone()), SlotState::Ready, false)
    }
}

impl Drop for Slot {
    // A held slot's bytes leave its job once the slot is freed
    fn drop(&mut self) {
        super::died(super::SLOTS);
        if let Some(h) = self.held.take() {
            if let Ok(mut q) = h.freed.try_borrow_mut() {
                q.push_back((h.job, h.size));
            }
        }
    }
}

/// ``slot``'s value counts ``size`` bytes toward ``job`` while the slot lives; the bytes added
/// (0 for an empty value or a slot already held).
pub fn hold(slot: &SlotRef, job: u64, size: i64, freed: &Freed) -> i64 {
    let mut s = slot.borrow_mut();
    if size <= 0 || s.held.is_some() {
        return 0;
    }
    s.held = Some(Held { job, size, freed: freed.clone() });
    size
}

pub fn deref(s: &SlotRef) -> SlotRef {
    let mut cur = s.clone();
    loop {
        let next = cur.borrow().forward.clone();
        match next {
            Some(n) => cur = n,
            None => return cur,
        }
    }
}

/// The version a reader at ``key`` sees: follow resolved phis and older `mutates` versions.
pub fn effective(s: &SlotRef, key: &Key) -> SlotRef {
    let mut s = deref(s);
    loop {
        let best = {
            let b = s.borrow();
            if b.muts.is_empty() {
                break;
            }
            let mut best: Option<(&Key, &SlotRef)> = None;
            for (k, v) in b.muts.iter() {
                if k < key
                    && v.borrow().state != SlotState::Squashed
                    && best.is_none_or(|(bk, _)| k > bk)
                {
                    best = Some((k, v));
                }
            }
            best.map(|(_, v)| v.clone())
        };
        match best {
            Some(v) => s = deref(&v),
            None => break,
        }
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_reader_sees_the_latest_older_unsquashed_version() {
        let base = Slot::pending();
        let v1 = Slot::pending();
        let v2 = Slot::pending();
        let v3 = Slot::pending();
        base.borrow_mut().muts.push((Key::of(&[], 2), v1.clone()));
        base.borrow_mut().muts.push((Key::of(&[], 5), v2.clone()));
        base.borrow_mut().muts.push((Key::of(&[], 4), v3.clone()));
        v2.borrow_mut().state = SlotState::Squashed;

        // Older than every version: the base; past 4 the version at 4 (5 is squashed)
        assert!(Rc::ptr_eq(&effective(&base, &Key::of(&[], 1)), &base));
        assert!(Rc::ptr_eq(&effective(&base, &Key::of(&[], 3)), &v1));
        assert!(Rc::ptr_eq(&effective(&base, &Key::of(&[], 9)), &v3));
    }

    #[test]
    fn a_resolved_phi_forwards_to_its_target() {
        let phi = Slot::phi();
        let mid = Slot::phi();
        let target = Slot::pending();
        phi.borrow_mut().forward = Some(mid.clone());
        mid.borrow_mut().forward = Some(target.clone());
        assert!(Rc::ptr_eq(&deref(&phi), &target));
        assert!(Rc::ptr_eq(&effective(&phi, &Key::last()), &target));
    }

    #[test]
    fn a_freed_held_slot_releases_its_bytes_once() {
        let freed: Freed = Rc::new(RefCell::new(VecDeque::new()));
        let s = Slot::pending();
        assert_eq!(hold(&s, 7, 100, &freed), 100);
        assert_eq!(hold(&s, 7, 50, &freed), 0);
        drop(s);
        assert_eq!(freed.borrow().iter().copied().collect::<Vec<_>>(), vec![(7, 100)]);
    }
}
