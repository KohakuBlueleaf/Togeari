//! Bindings of the contracts: `togeari._rs.digest` and `digest_with_weak`, the native digest as
//! the core computes it (compared against the Python digest by the tests).

use pyo3::prelude::*;
use pyo3::types::PyBytes;

use super::digest::Digests;

#[pyfunction]
fn digest<'py>(py: Python<'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let d = Digests::new(py)?.digest(value)?;
    Ok(PyBytes::new(py, &d))
}

#[pyfunction]
fn digest_with_weak<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyBytes>, Vec<String>)> {
    let (d, weak) = Digests::new(py)?.digest_with_weak(value)?;
    Ok((PyBytes::new(py, &d), weak))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(digest, m)?)?;
    m.add_function(wrap_pyfunction!(digest_with_weak, m)?)?;
    Ok(())
}
