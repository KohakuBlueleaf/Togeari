//! The canonical digest (`togeari.core.contracts.digest`), fed natively for builtin values:
//! scalars, strings, bytes and the builtin containers. Any other value, and a container type a
//! digest is registered for, is fed by the Python digest's own code (`feed_bytes`).

use std::cmp::Ordering;

use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyComplex, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet,
    PyString, PyTuple,
};

use super::hash::Sha256;
use crate::runtime::state::Obj;

pub struct Digests {
    registry: Py<PyDict>,
    registered: Obj,
    feed_bytes: Obj,
}

/// The bytes one piece adds: a tag, the data's length, the data.
fn chunk(h: &mut Sha256, tag: u8, data: &[u8]) {
    h.update(&[tag]);
    h.update(&(data.len() as u64).to_le_bytes());
    h.update(data);
}

/// A dict key's or set member's place in a container's order: strings by value, anything else
/// by its own digest.
enum Place {
    S(String),
    D([u8; 32]),
}

impl Place {
    fn cmp(&self, o: &Place) -> Ordering {
        match (self, o) {
            (Place::S(a), Place::S(b)) => a.cmp(b),
            (Place::S(_), Place::D(_)) => Ordering::Less,
            (Place::D(_), Place::S(_)) => Ordering::Greater,
            (Place::D(a), Place::D(b)) => a.cmp(b),
        }
    }
}

struct Feed<'a> {
    d: &'a Digests,
    h: Sha256,
    weak: Vec<String>,
    // Ids of the containers being fed
    open: Vec<usize>,
}

impl Digests {
    pub fn new(py: Python<'_>) -> PyResult<Digests> {
        let m = py.import("togeari.core.contracts.digest")?;
        Ok(Digests {
            registry: m.getattr("_REGISTRY")?.cast_into::<PyDict>()?.unbind(),
            registered: m.getattr("_registered")?.unbind(),
            feed_bytes: m.getattr("feed_bytes")?.unbind(),
        })
    }

    pub fn digest(&self, v: &Bound<'_, PyAny>) -> PyResult<[u8; 32]> {
        Ok(self.digest_with_weak(v)?.0)
    }

    /// The digest and the type names compared by type only.
    pub fn digest_with_weak(&self, v: &Bound<'_, PyAny>) -> PyResult<([u8; 32], Vec<String>)> {
        let mut f = Feed { d: self, h: Sha256::default(), weak: Vec::new(), open: Vec::new() };
        f.feed(v)?;
        Ok((f.h.digest(), f.weak))
    }

    /// Whether a digest is registered for ``t`` (or a base of it).
    fn registered(&self, v: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = v.py();
        if self.registry.bind(py).is_empty() {
            return Ok(false);
        }
        Ok(!self.registered.bind(py).call1((v.get_type(),))?.is_none())
    }

    pub fn visit(&self, visit: &pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.registry)?;
        visit.call(&self.registered)?;
        visit.call(&self.feed_bytes)
    }
}

impl Feed<'_> {
    fn update(&mut self, s: &str) {
        self.h.update(s.as_bytes());
    }

    /// ``value`` fed by the Python digest's code, with this feed's open containers.
    fn python(&mut self, v: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = v.py();
        let weak = PyList::empty(py);
        let open = if self.open.is_empty() {
            py.None().into_bound(py)
        } else {
            PySet::new(py, self.open.iter())?.into_any()
        };
        let b = self.d.feed_bytes.bind(py).call1((v, &weak, open))?;
        self.h.update(b.cast::<PyBytes>()?.as_bytes());
        for t in weak.iter() {
            self.weak.push(t.extract()?);
        }
        Ok(())
    }

    fn scalar(&mut self, name: &str, repr: &str) {
        chunk(&mut self.h, b'v', format!("{name}:{repr}").as_bytes());
    }

    fn feed(&mut self, v: &Bound<'_, PyAny>) -> PyResult<()> {
        // Scalars
        if v.is_none() {
            self.scalar("NoneType", "None");
            return Ok(());
        }
        if v.is_exact_instance_of::<PyBool>() {
            let b = v.is_truthy()?;
            self.scalar("bool", if b { "True" } else { "False" });
            return Ok(());
        }
        if v.is_exact_instance_of::<PyInt>() {
            match v.extract::<i64>() {
                Ok(i) => self.scalar("int", &i.to_string()),
                Err(_) => self.scalar("int", &v.repr()?.to_string()),
            }
            return Ok(());
        }
        if v.is_exact_instance_of::<PyFloat>() {
            self.scalar("float", &v.repr()?.to_string());
            return Ok(());
        }
        if v.is_exact_instance_of::<PyComplex>() {
            self.scalar("complex", &v.repr()?.to_string());
            return Ok(());
        }
        if v.is_exact_instance_of::<PyString>() {
            match v.cast::<PyString>()?.to_str() {
                Ok(s) => chunk(&mut self.h, b's', s.as_bytes()),
                Err(_) => self.python(v)?,
            }
            return Ok(());
        }
        if v.is_exact_instance_of::<PyBytes>() {
            chunk(&mut self.h, b'b', v.cast::<PyBytes>()?.as_bytes());
            return Ok(());
        }
        if v.is_exact_instance_of::<PyByteArray>() {
            let data = v.cast::<PyByteArray>()?.to_vec();
            chunk(&mut self.h, b'b', &data);
            return Ok(());
        }

        // The builtin containers, recursively, unless a digest is registered for them
        let seq = v.is_exact_instance_of::<PyList>() || v.is_exact_instance_of::<PyTuple>();
        let map = v.is_exact_instance_of::<PyDict>();
        let set = v.is_exact_instance_of::<PySet>() || v.is_exact_instance_of::<PyFrozenSet>();
        if !(seq || map || set) || self.d.registered(v)? {
            return self.python(v);
        }
        let id = v.as_ptr() as usize;
        if self.open.contains(&id) {
            let t = v.get_type().qualname()?.to_string();
            chunk(&mut self.h, b'w', format!("weak:{t}").as_bytes());
            self.weak.push(t);
            return Ok(());
        }
        let n = v.len()?;
        if seq {
            let name = if v.is_exact_instance_of::<PyList>() {
                "list"
            } else {
                "tuple"
            };
            self.open.push(id);
            self.update(&format!("{name}[{n}]("));
            let items: Vec<Bound<'_, PyAny>> = v.try_iter()?.collect::<PyResult<_>>()?;
            for x in &items {
                self.feed(x)?;
            }
        } else {
            let Some(members) = ordered(self.d, v)? else {
                return self.python(v);
            };
            self.open.push(id);
            if map {
                self.update(&format!("dict[{n}]("));
                let d = v.cast::<PyDict>()?;
                for k in &members {
                    self.feed(k)?;
                    let x = d
                        .get_item(k)?
                        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(k.clone().unbind()))?;
                    self.feed(&x)?;
                }
            } else {
                self.update(&format!("set[{n}]("));
                for k in &members {
                    self.feed(k)?;
                }
            }
        }
        self.update(")");
        self.open.pop();
        Ok(())
    }
}

/// A dict's keys or a set's members in the digest's order; None when one needs Python's own
/// comparison (a ``str`` subclass, or a string Rust cannot hold).
fn ordered<'py>(d: &Digests, v: &Bound<'py, PyAny>) -> PyResult<Option<Vec<Bound<'py, PyAny>>>> {
    let mut keyed: Vec<(Place, Bound<'py, PyAny>)> = Vec::new();
    for k in v.try_iter()? {
        let k = k?;
        let place = if k.is_instance_of::<PyString>() {
            if !k.is_exact_instance_of::<PyString>() {
                return Ok(None);
            }
            match k.cast::<PyString>()?.to_str() {
                Ok(s) => Place::S(s.to_string()),
                Err(_) => return Ok(None),
            }
        } else {
            Place::D(d.digest(&k)?)
        };
        keyed.push((place, k));
    }
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(Some(keyed.into_iter().map(|(_, k)| k).collect()))
}
