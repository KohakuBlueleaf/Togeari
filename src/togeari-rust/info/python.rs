//! Bindings of `info`: `__version__`, `DISPATCHER` and `core_info()`.

use pyo3::prelude::*;
use pyo3::types::PyDict;

use super::core;

/// The extension's description: `version`, `dispatcher` (the native dispatcher is built) and
/// `debug` (a debug build).
#[pyfunction]
fn core_info(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let info = core::core_info();
    let d = PyDict::new(py);
    d.set_item("version", info.version)?;
    d.set_item("dispatcher", info.dispatcher)?;
    d.set_item("debug", info.debug)?;
    Ok(d)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", core::VERSION)?;
    m.add("DISPATCHER", core::DISPATCHER)?;
    m.add_function(wrap_pyfunction!(core_info, m)?)?;
    Ok(())
}
