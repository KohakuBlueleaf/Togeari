//! Order keys: tuples of integers compared lexicographically, as the Python core's tuples.

use std::rc::Rc;

use pyo3::prelude::*;
use pyo3::types::PyTuple;

/// A strand's key prefix (the tuple before its counter).
pub type Prefix = Rc<[i64]>;

/// One statement's order key.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Key(pub Rc<[i64]>);

impl Key {
    /// `(*prefix, counter)`.
    pub fn of(prefix: &[i64], counter: i64) -> Key {
        let mut v = Vec::with_capacity(prefix.len() + 1);
        v.extend_from_slice(prefix);
        v.push(counter);
        Key(v.into())
    }

    /// A key above every statement's key (the Python core's `(inf,)`).
    pub fn last() -> Key {
        Key(Rc::from([i64::MAX]))
    }

    /// `(*self, x)` as a prefix.
    pub fn extend(&self, x: i64) -> Prefix {
        let mut v = Vec::with_capacity(self.0.len() + 1);
        v.extend_from_slice(&self.0);
        v.push(x);
        v.into()
    }

    pub fn to_py<'py>(&self, py: Python<'py>) -> Bound<'py, PyTuple> {
        PyTuple::new(py, self.0.iter()).expect("a tuple of ints")
    }
}

pub fn prefix_to_py<'py>(py: Python<'py>, p: &[i64]) -> Bound<'py, PyTuple> {
    PyTuple::new(py, p.iter()).expect("a tuple of ints")
}

pub fn empty_prefix() -> Prefix {
    Rc::from([])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_compare_as_tuples() {
        let a = Key::of(&[0], 1);
        let b = Key::of(&[0, 1], 0);
        let c = Key::of(&[1], 0);
        assert!(a < b && b < c);
        assert!(Key::of(&[], 5) < Key::last());
        assert_eq!(Key::of(&[2], 3).extend(1).as_ref(), &[2, 3, 1]);
    }
}
