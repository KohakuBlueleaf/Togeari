//! Bindings of `telemetry`: `TelemetryRing`, the methods of `togeari.core.observe.ring.Ring`.

use std::path::PathBuf;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

use super::core::{record, Put, Ring, DEFAULT_RECORDS};

#[pyclass(name = "TelemetryRing", module = "togeari._rs", frozen)]
pub struct TelemetryRing {
    pub ring: Arc<Ring>,
}

fn unit_kind(kind: &str) -> PyResult<bool> {
    match kind {
        "lane" => Ok(false),
        "unit" => Ok(true),
        _ => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "a name kind is 'lane' or 'unit', not {kind:?}"
        ))),
    }
}

#[pymethods]
impl TelemetryRing {
    /// A ring of `capacity` records: on the heap, or with `path` in a new file mapping that
    /// another process reads through `open(path)` (the file is removed with this ring).
    #[new]
    #[pyo3(signature = (capacity = DEFAULT_RECORDS, path = None))]
    fn new(capacity: usize, path: Option<PathBuf>) -> PyResult<Self> {
        let ring = match path {
            None => Ring::new(capacity),
            Some(p) => Ring::create(&p, capacity)?,
        };
        Ok(TelemetryRing { ring: Arc::new(ring) })
    }

    /// The ring another process created at `path`, to drain.
    #[staticmethod]
    fn open(path: PathBuf) -> PyResult<Self> {
        Ok(TelemetryRing { ring: Arc::new(Ring::open(&path)?) })
    }

    #[getter]
    fn capacity(&self) -> usize {
        self.ring.capacity()
    }

    /// The mapped file, None on the heap.
    #[getter]
    fn path(&self) -> Option<PathBuf> {
        self.ring.path().map(|p| p.to_path_buf())
    }

    #[getter]
    fn epoch_wall(&self) -> f64 {
        self.ring.epoch_wall
    }

    fn now_ns(&self) -> u64 {
        self.ring.now_ns()
    }

    /// Write one record now; lane and unit by name (None: none).
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (kind, job, addr, lane, unit, queued_s, service_s, aux, status))]
    fn put(
        &self,
        kind: u8,
        job: u64,
        addr: Option<i64>,
        lane: Option<&str>,
        unit: Option<&str>,
        queued_s: Option<f64>,
        service_s: Option<f64>,
        aux: i64,
        status: u8,
    ) {
        let aux = aux as u64;
        self.ring
            .put(&Put { kind, job, addr, lane, unit, queued_s, service_s, aux, status });
    }

    /// The records written since the last drain (up to `max_records`), oldest first; the GIL
    /// is released for the copy.
    #[pyo3(signature = (max_records = 1 << 30))]
    fn drain<'py>(&self, py: Python<'py>, max_records: usize) -> Bound<'py, PyBytes> {
        let ring = self.ring.clone();
        let out = py.detach(move || ring.drain(max_records));
        PyBytes::new(py, &out)
    }

    fn name_id(&self, kind: &str, name: &str) -> PyResult<u16> {
        Ok(self.ring.name_id(unit_kind(kind)?, name))
    }

    fn leave(&self, kind: &str, name: &str) -> PyResult<()> {
        let unit = unit_kind(kind)?;
        let t = self.ring.now_ns();
        if let Ok(mut n) = self.ring.names.lock() {
            n.leave(unit, name, t);
        }
        Ok(())
    }

    /// Every `(kind, id, name, appeared_ns, left_ns)`.
    fn name_rows<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let rows = self
            .ring
            .names
            .lock()
            .map(|n| n.rows.clone())
            .unwrap_or_default();
        PyList::new(py, rows)
    }

    fn counts<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let r = &self.ring;
        let d = PyDict::new(py);
        d.set_item("written", r.written())?;
        d.set_item("dropped", r.dropped())?;
        d.set_item("saturated", r.saturated())?;
        d.set_item("pending", r.pending())?;
        d.set_item("capacity", r.capacity())?;
        Ok(d)
    }

    /// One record's bytes, as `togeari.core.observe.ring.pack` makes them.
    #[staticmethod]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (t_ns, kind, job, addr, lane, unit, queued_s, service_s, aux, status))]
    fn pack<'py>(
        py: Python<'py>,
        t_ns: u64,
        kind: u8,
        job: u64,
        addr: Option<i64>,
        lane: u16,
        unit: u16,
        queued_s: Option<f64>,
        service_s: Option<f64>,
        aux: i64,
        status: u8,
    ) -> Bound<'py, PyBytes> {
        let aux = aux as u64;
        let p = Put { kind, job, addr, lane: None, unit: None, queued_s, service_s, aux, status };
        PyBytes::new(py, &record(t_ns, &p, lane, unit).bytes())
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<TelemetryRing>()?;
    m.add("TELEMETRY_RECORD_BYTES", super::core::SIZE)?;
    Ok(())
}
