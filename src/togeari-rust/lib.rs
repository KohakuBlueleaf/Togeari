//! `togeari._rs`: a second implementation of the protocol the Python runtime defines.
//! Modules mirror `src/togeari/core/`; bindings live in each module's `python.rs`.

use pyo3::prelude::*;

mod contracts;
mod info;
mod lanes;
mod program;
mod runtime;

#[pymodule(gil_used = true)]
fn _rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    info::python::register(m)?;
    contracts::python::register(m)?;
    runtime::python::register(m)?;
    lanes::python::register(m)?;
    Ok(())
}
