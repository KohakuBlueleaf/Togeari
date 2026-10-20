//! `togeari._rs`: a second implementation of the protocol the Python runtime defines.
//! Modules mirror `src/togeari/core/`; each splits pure Rust (`core.rs`) from bindings
//! (`python.rs`).

use pyo3::prelude::*;

mod info;

#[pymodule]
fn _rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    info::python::register(m)?;
    Ok(())
}
