//! A job's inflights by order key: a list with a moving head (retire pops the head).

use super::inflight::InfRef;
use super::key::Key;

#[derive(Default)]
pub struct Window {
    pub items: Vec<InfRef>,
    pub head: usize,
}

impl Window {
    pub fn add(&mut self, inf: InfRef) {
        let key = inf.borrow().key.clone();
        let append = self.items.len() == self.head
            || self.items.last().is_some_and(|x| x.borrow().key < key);
        if append {
            self.items.push(inf);
            return;
        }

        // After every item with a key not above it (bisect right)
        let at = self.head + self.items[self.head..].partition_point(|x| x.borrow().key <= key);
        self.items.insert(at, inf);
    }

    pub fn first(&self) -> Option<InfRef> {
        self.items.get(self.head).cloned()
    }

    pub fn pop_first(&mut self) -> InfRef {
        let x = self.items[self.head].clone();
        self.head += 1;
        if self.head > 256 && self.head * 2 > self.items.len() {
            self.items.drain(..self.head);
            self.head = 0;
        }
        x
    }

    /// Remove and return every inflight with a key larger than ``key``.
    pub fn cut_after(&mut self, key: &Key) -> Vec<InfRef> {
        let lo = self.head + self.items[self.head..].partition_point(|x| x.borrow().key <= *key);
        self.items.split_off(lo)
    }

    pub fn clear(&mut self) -> Vec<InfRef> {
        let rest = self.items.split_off(self.head);
        self.items = Vec::new();
        self.head = 0;
        rest
    }

    pub fn len(&self) -> usize {
        self.items.len() - self.head
    }

    pub fn is_empty(&self) -> bool {
        self.items.len() == self.head
    }

    pub fn live(&self) -> &[InfRef] {
        &self.items[self.head..]
    }
}
