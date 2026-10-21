//! A lane's former and selector as one lane operation runs them, read live from the policy
//! objects: builtin method bodies natively, anything else called back (spec/11 §11.4).

use pyo3::intern;
use pyo3::prelude::*;

use crate::runtime::builtins::{plain_f64, Builtins, Tag};
use crate::runtime::state::Obj;

/// How the former keys an item.
pub enum KeyK {
    // The contract default: the unit's own key, None when it has none or raises
    Default,
    Bucketed { inner: Box<KeyK>, key_fn: Obj },
    User(Obj),
}

/// How the former gives an item's due time.
pub enum DueK {
    Never,
    Sow { wait: f64 },
    User(Obj),
}

/// How the former gives a bucket's deadline.
pub enum DeadlineK {
    // `READY` for a non-empty bucket
    Ready,
    Sow { size: f64 },
    User(Obj),
}

/// How the former says how many items a submit carries.
pub enum TakeK {
    // The contract's body returns None: nothing
    Nothing,
    Greedy,
    Sow { size: f64 },
    User(Obj),
}

/// The former of one lane operation.
pub struct FormerNow {
    pub key: KeyK,
    pub due: DueK,
    pub deadline: DeadlineK,
    pub take: TakeK,
}

/// How the selector orders units.
pub enum OrderK {
    // `list(units)`
    Given,
    LeastLoaded,
    Throughput,
    RoundRobin(Obj),
    User(Obj),
}

/// How the selector admits an item to a unit.
pub enum AdmitsK {
    All,
    ByHint,
    Affinity(Obj),
    User(Obj),
}

pub struct SelectNow {
    pub order: OrderK,
    pub admits: AdmitsK,
}

fn num(o: &Bound<'_, PyAny>, name: &str) -> PyResult<Option<f64>> {
    Ok(plain_f64(&o.getattr(name)?))
}

impl FormerNow {
    pub fn of(py: Python<'_>, b: &Builtins, f: &Bound<'_, PyAny>) -> PyResult<FormerNow> {
        Ok(FormerNow {
            key: key_k(py, b, f)?,
            due: due_k(py, b, f)?,
            deadline: deadline_k(py, b, f)?,
            take: take_k(py, b, f)?,
        })
    }
}

fn key_k(py: Python<'_>, b: &Builtins, f: &Bound<'_, PyAny>) -> PyResult<KeyK> {
    Ok(match b.method(py, f, "key")? {
        Some(Tag::FormerKey) => KeyK::Default,
        Some(Tag::BucketedKey) => {
            let inner = f.getattr(intern!(py, "inner"))?;
            KeyK::Bucketed {
                inner: Box::new(key_k(py, b, &inner)?),
                key_fn: f.getattr(intern!(py, "key_fn"))?.unbind(),
            }
        }
        _ => KeyK::User(f.clone().unbind()),
    })
}

fn due_k(py: Python<'_>, b: &Builtins, f: &Bound<'_, PyAny>) -> PyResult<DueK> {
    Ok(match b.method(py, f, "due")? {
        Some(Tag::FormerDue) => DueK::Never,
        Some(Tag::SowDue) if b.method(py, f, "_limit")? == Some(Tag::SowLimit) => {
            match num(f, "wait")? {
                Some(wait) => DueK::Sow { wait },
                None => DueK::User(f.clone().unbind()),
            }
        }
        Some(Tag::BucketedDue) => due_k(py, b, &f.getattr(intern!(py, "inner"))?)?,
        _ => DueK::User(f.clone().unbind()),
    })
}

fn deadline_k(py: Python<'_>, b: &Builtins, f: &Bound<'_, PyAny>) -> PyResult<DeadlineK> {
    Ok(match b.method(py, f, "deadline")? {
        Some(Tag::FormerDeadline | Tag::GreedyDeadline) => DeadlineK::Ready,
        Some(Tag::SowDeadline) => match num(f, "size")? {
            Some(size) => DeadlineK::Sow { size },
            None => DeadlineK::User(f.clone().unbind()),
        },
        Some(Tag::BucketedDeadline) => deadline_k(py, b, &f.getattr(intern!(py, "inner"))?)?,
        _ => DeadlineK::User(f.clone().unbind()),
    })
}

fn take_k(py: Python<'_>, b: &Builtins, f: &Bound<'_, PyAny>) -> PyResult<TakeK> {
    Ok(match b.method(py, f, "take")? {
        Some(Tag::FormerTake) => TakeK::Nothing,
        Some(Tag::GreedyTake) => TakeK::Greedy,
        Some(Tag::SowTake) => match num(f, "size")? {
            Some(size) => TakeK::Sow { size },
            None => TakeK::User(f.clone().unbind()),
        },
        Some(Tag::BucketedTake) => take_k(py, b, &f.getattr(intern!(py, "inner"))?)?,
        _ => TakeK::User(f.clone().unbind()),
    })
}

impl SelectNow {
    pub fn of(py: Python<'_>, b: &Builtins, s: &Bound<'_, PyAny>) -> PyResult<SelectNow> {
        Ok(SelectNow { order: order_k(py, b, s)?, admits: admits_k(py, b, s)? })
    }
}

fn order_k(py: Python<'_>, b: &Builtins, s: &Bound<'_, PyAny>) -> PyResult<OrderK> {
    Ok(match b.method(py, s, "order")? {
        Some(Tag::SelectOrder) => OrderK::Given,
        Some(Tag::LeastLoadedOrder) => OrderK::LeastLoaded,
        Some(Tag::ThroughputOrder) => OrderK::Throughput,
        Some(Tag::RoundRobinOrder) => OrderK::RoundRobin(s.clone().unbind()),
        Some(Tag::FallbackOrder) => order_k(py, b, &s.getattr(intern!(py, "fallback"))?)?,
        _ => OrderK::User(s.clone().unbind()),
    })
}

fn admits_k(py: Python<'_>, b: &Builtins, s: &Bound<'_, PyAny>) -> PyResult<AdmitsK> {
    Ok(match b.method(py, s, "admits")? {
        Some(Tag::SelectAdmits) => AdmitsK::All,
        Some(Tag::ByHintAdmits) => AdmitsK::ByHint,
        Some(Tag::AffinityAdmits)
            if b.method(py, s, "preferred")? == Some(Tag::AffinityPreferred)
                && b.method(py, s, "item_key")? == Some(Tag::AffinityItemKey) =>
        {
            AdmitsK::Affinity(s.clone().unbind())
        }
        _ => AdmitsK::User(s.clone().unbind()),
    })
}
