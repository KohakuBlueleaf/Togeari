//! The dispatcher cycle: inbox, fetch, issue, pump, retire, admit (spec/03 §3.11).

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use super::backend::issue::IssueNow;
use super::core::Core;
use super::frontend::fetch::Flow;
use super::host::{is_exception, K};
use super::retire::retire::{RetireCaps, RetirePass};
use super::state::InfRef;
use crate::lanes::lane::{NEXT_DEADLINE, PUMP, RESULTS, TIMERS};

// Cycles between two `dispatcher.cycle` events
const CYCLE_EVENT_EVERY: i64 = 100;

/// Core state carried into a GIL-free region on the dispatcher thread itself: no other thread
/// touches it while the gate is set (readers wait in ``outside_region``).
struct Unshared<T>(T);

unsafe impl<T> Send for Unshared<T> {}

impl Core {
    // ------------------------------------------------------------ waiting
    /// 0 with work ready now; the time to the nearest lane deadline; None to wait for events.
    pub fn next_timeout(&self, py: Python<'_>) -> PyResult<Option<f64>> {
        if !self.dirty.borrow().is_empty() || !self.ready.borrow().is_empty() {
            return Ok(Some(0.0));
        }
        let now = self.now(py)?;
        let mut nearest: Option<f64> = None;
        for lane in self.h.lanes.bind(py).try_iter()? {
            let lane = lane?;
            let d = match self.native_for(py, &lane, NEXT_DEADLINE)? {
                Some(n) => n.get().c().next_deadline(py, now)?,
                None => {
                    let d = lane.call_method1(intern!(py, "next_deadline"), (now,))?;
                    crate::lanes::lane::opt_f64(&d)?
                }
            };
            if let Some(d) = d {
                if nearest.is_none_or(|n| d < n) {
                    nearest = Some(d);
                }
            }
        }
        Ok(nearest.map(|n| (n - now).max(0.0)))
    }

    fn inbox(&self, py: Python<'_>) -> Py<super::inbox::Inbox> {
        self.inbox
            .borrow()
            .as_ref()
            .expect("a bound inbox")
            .clone_ref(py)
    }

    // ------------------------------------------------------------ events
    pub fn event_error(&self, py: Python<'_>, kind: &str, e: &PyErr) {
        self.h
            .log(py, "error", &format!("dispatcher event {kind} failed"), Some(e));
        self.stats.borrow_mut().machine_errors += 1;
    }

    /// Results from a lane: settle them in the lane, deliver each.
    fn done(
        &self,
        py: Python<'_>,
        lane: &Bound<'_, PyAny>,
        sub: &Bound<'_, PyAny>,
        results: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let now = self.now(py)?;

        // A native lane names a submission by id; its items and unit come from the lane
        let adopted = self.native_lane(py, lane)?;
        let (items, unit) = match &adopted {
            Some(n) => match n.get().c().sub_items(py, sub.extract()?) {
                Some((items, unit)) => (PyList::new(py, items)?.into_any(), unit.into_bound(py)),
                None => (PyList::empty(py).into_any(), py.None().into_bound(py)),
            },
            None => (sub.getattr(intern!(py, "items"))?, sub.getattr(intern!(py, "unit"))?),
        };
        let native = adopted.filter(|n| n.get().c().overridden.get() & RESULTS == 0);
        let got = match &native {
            Some(n) => n
                .get()
                .c()
                .results(py, sub.extract()?, results, now)
                .and_then(|pairs| PyList::new(py, pairs).map(|l| l.into_any())),
            None => lane.call_method1(intern!(py, "results"), (sub, results, now)),
        };
        let settled = match got {
            Ok(s) => s,
            Err(e) if is_exception(py, &e) => {
                self.event_error(py, "done", &e);
                let out = PyList::empty(py);
                let err = self.h.item_error.bind(py).call1((e.value(py),))?;
                for it in items.try_iter()? {
                    let it = it?;
                    it.getattr(intern!(py, "ext"))?.set_item("settled", true)?;
                    out.append((it, &err))?;
                }
                out.into_any()
            }
            Err(e) => return Err(e),
        };
        for pair in settled.try_iter()? {
            let pair = pair?;
            let (it, r) = (pair.get_item(0)?, pair.get_item(1)?);
            self.deliver(py, &it, &r, Some(&unit))?;
        }
        Ok(())
    }

    fn handle(&self, py: Python<'_>, ev: &Bound<'_, PyAny>) -> PyResult<()> {
        let Ok(t) = ev.cast::<PyTuple>() else {
            return Ok(());
        };
        if t.is_empty() {
            return Ok(());
        }
        let tag = t.get_item(0)?;
        let Ok(tag) = tag.extract::<String>() else {
            return Ok(());
        };
        let n = t.len();
        match (tag.as_str(), n) {
            // Results from a lane
            ("done", 4) => self.done(py, &t.get_item(1)?, &t.get_item(2)?, &t.get_item(3)?)?,

            // Requests from other threads: jobs, queries, commands, calls
            ("submit", 3) => {
                let (item, handle) = (t.get_item(1)?, t.get_item(2)?);
                let h = (!handle.is_none()).then_some(&handle);
                match self.start_job(py, &item, h) {
                    Ok(()) => {}
                    Err(e) if is_exception(py, &e) => {
                        let key = item.getattr(intern!(py, "key"))?.str()?.to_string();
                        self.h
                            .log(py, "error", &format!("job {key} could not start"), Some(&e));
                        self.stats.borrow_mut().machine_errors += 1;
                        let ty = e.get_type(py).name()?.to_string();
                        let msg = format!("{ty}: {}", e.value(py).str()?);
                        self.reject(py, &item, "MachineError", &msg, h, None)?;
                    }
                    Err(e) => return Err(e),
                }
            }
            ("query", 4) | ("command", 4) => {
                let name = if tag == "query" {
                    "queries"
                } else {
                    "commands"
                };
                let table = self.h.machine.bind(py).getattr(name)?;
                let shell = self.shell(py)?;
                shell.call_method1(
                    intern!(py, "answer"),
                    (table, tag.as_str(), t.get_item(1)?, t.get_item(2)?, t.get_item(3)?),
                )?;
            }
            ("call", 2) | ("call", 3) => {
                let owner = if n == 3 { Some(t.get_item(2)?) } else { None };
                let owner = owner.filter(|o| !o.is_none());
                match t.get_item(1)?.call0() {
                    Ok(_) => {}
                    Err(e) if is_exception(py, &e) => match owner {
                        None => self.event_error(py, "call", &e),
                        Some(o) => {
                            self.h.plugins.bind(py).call_method1(
                                intern!(py, "failed_by_name"),
                                (o, "post", e.value(py)),
                            )?;
                        }
                    },
                    Err(e) => return Err(e),
                }
            }

            // Runs, wake-ups and stop
            ("run", 4) => {
                let shell = self.shell(py)?;
                shell.call_method1(
                    intern!(py, "attach_run"),
                    (t.get_item(1)?, t.get_item(2)?, t.get_item(3)?),
                )?;
            }
            ("stop", 3) => {
                self.begin_stop(py, t.get_item(1)?.is_truthy()?)?;
                t.get_item(2)?.call_method1(intern!(py, "_set"), (true,))?;
            }
            _ => {}
        }
        Ok(())
    }

    // ------------------------------------------------------------ the collector
    fn apply_collector(&self, py: Python<'_>, shell: &Bound<'_, PyAny>) -> PyResult<()> {
        let want = self.h.knob(py, K::GcThreshold)?;
        let set = shell.getattr(intern!(py, "gc_set"))?;
        if want.is(&set) || want.eq(&set)? {
            return Ok(());
        }
        shell.call_method0(intern!(py, "apply_collector"))?;
        Ok(())
    }

    /// Whether this cycle may release the GIL: the knob is on and no bus listener acts on events
    /// as they come (a passive one receives them when the region ends).
    fn regions_ok(&self, py: Python<'_>) -> PyResult<bool> {
        if !self.h.knob(py, K::ReleaseGil)?.is_truthy()? {
            return Ok(false);
        }
        let acting = self.h.bus.bind(py).getattr(intern!(py, "acting"))?;
        Ok(!acting.is_truthy()?)
    }

    // ------------------------------------------------------------ the cycle
    pub fn cycle(&self, py: Python<'_>) -> PyResult<()> {
        let shell = self.shell(py)?;
        self.apply_collector(py, &shell)?;

        // Inbox: wait for the first event (idle time), then drain the rest in batches
        let t0 = self.perf();
        let timeout = self.next_timeout(py)?;
        let busy = self.perf() - t0;
        let inbox = self.inbox(py);
        let mut rest = std::collections::VecDeque::new();
        let first = match timeout {
            Some(t) if t <= 0.0 => {
                rest = inbox.get().take_all();
                rest.pop_front()
            }
            t => inbox.get().wait(py, t)?,
        };
        let t_cycle = self.perf();
        if let Some(ev) = first {
            self.handle(py, ev.bind(py))?;
        }
        loop {
            if rest.is_empty() {
                rest = inbox.get().take_all();
                if rest.is_empty() {
                    break;
                }
            }
            while let Some(ev) = rest.pop_front() {
                self.handle(py, ev.bind(py))?;
            }
        }
        if shell.getattr(intern!(py, "sink_pending"))?.is_truthy()? {
            shell.call_method0(intern!(py, "flush_sink"))?;
        }
        let t1 = self.perf();
        self.add_phase("drain", t1 - t_cycle);

        // Fetch for jobs with runnable strands; with regions, the issue order is taken in the
        // fetch region when the policy needs no Python
        let dirty = std::mem::take(&mut *self.dirty.borrow_mut());
        let ids: Vec<u64> = dirty.iter().copied().collect();
        let free = self.regions_ok(py)?;
        let issue = self.issue_now(py)?;
        let mut ordered = None;
        if free {
            self.bus_quiet.set(self.quiet(py));
            self.bind_lanes(py)?;
            ordered = self.fetch_released(py, &ids, &issue)?;
        } else {
            let caps = self.caps(py)?;
            for jid in &ids {
                let Some(job) = self.job(*jid) else { continue };
                if self.fetchable(&job) {
                    let mut pass = Core::pass(&job);
                    match self.run_pass(Some(py), &job, &mut pass, &caps) {
                        Ok(_) => {}
                        Err(e) if is_exception(py, &e) => self.job_error(py, &job, &e)?,
                        Err(e) => return Err(e),
                    }
                }
            }
        }
        let t2 = self.perf();
        self.add_phase("fetch", t2 - t1);

        // Issue ready statements, then multi-issue to units
        self.issue(py, &issue, ordered)?;
        let t3 = self.perf();
        self.add_phase("issue", t3 - t2);
        let now = self.now(py)?;
        for lane in self.h.lanes.bind(py).try_iter()? {
            let lane = lane?;
            match self.native_for(py, &lane, PUMP | TIMERS)? {
                Some(n) => {
                    n.get().c().pump(py, now)?;
                    for (it, r) in n.get().c().timers(py, now)? {
                        self.deliver(py, it.bind(py), r.bind(py), None)?;
                    }
                }
                None => {
                    lane.call_method1(intern!(py, "pump"), (now,))?;
                    for pair in lane
                        .call_method1(intern!(py, "timers"), (now,))?
                        .try_iter()?
                    {
                        let pair = pair?;
                        self.deliver(py, &pair.get_item(0)?, &pair.get_item(1)?, None)?;
                    }
                }
            }
        }
        let t4 = self.perf();
        self.add_phase("pump", t4 - t3);

        // Retire in program order; finished jobs go to their handles or the sink
        let order: Vec<u64> = dirty.iter().copied().collect();
        if free {
            self.retire_released(py, &order)?;
        } else {
            let caps = self.retire_caps(py)?;
            for jid in &order {
                let Some(job) = self.job(*jid) else { continue };
                if job.borrow().ended {
                    continue;
                }
                let mut pass = RetirePass::new(&job);
                match self.retire(Some(py), &job, &mut pass, &caps) {
                    Ok(_) => {}
                    Err(e) if is_exception(py, &e) => self.job_error(py, &job, &e)?,
                    Err(e) => return Err(e),
                }
            }
        }
        let t5 = self.perf();
        self.add_phase("retire", t5 - t4);

        // Admission
        self.admit(py)?;
        let t6 = self.perf();
        self.add_phase("admit", t6 - t5);
        self.sweep_owned();

        // Cycle totals and time histogram: bucket = bit length of the cycle's microseconds
        let dt = t6 - t_cycle;
        let cycles = {
            let mut s = self.stats.borrow_mut();
            s.cycles += 1;
            s.busy_s += busy + dt;
            let us = (dt * 1e6).max(0.0) as u64;
            let bucket = 64 - us.leading_zeros();
            *s.cycle_hist.entry(bucket).or_insert(0) += 1;
            if dt > s.cycle_max_s {
                s.cycle_max_s = dt;
            }
            s.cycles
        };
        if cycles % CYCLE_EVENT_EVERY == 0 && self.wants(py, "dispatcher.cycle", None)? {
            let phase: Vec<(&'static str, f64)> = self
                .stats
                .borrow()
                .phase_s
                .iter()
                .map(|(k, v)| (*k, *v))
                .collect();
            let f = PyDict::new(py);
            f.set_item("cycles", cycles)?;
            let p = PyDict::new(py);
            for (k, v) in phase {
                p.set_item(k, v)?;
            }
            f.set_item("phase_s", p)?;
            self.emit(py, "dispatcher.cycle", &f)?;
        }
        Ok(())
    }

    /// Every machine lane known to the core (native ones learn the region gate).
    fn bind_lanes(&self, py: Python<'_>) -> PyResult<()> {
        if !self.lanes.borrow().is_empty() {
            return Ok(());
        }
        for lane in self.h.lanes.bind(py).try_iter()? {
            self.native_lane(py, &lane?)?;
        }
        Ok(())
    }

    fn fetchable(&self, job: &super::state::JobRef) -> bool {
        let j = job.borrow();
        !j.ended && !j.paused
    }

    /// Fetch for ``ids`` in order with the GIL released; a job whose pass meets a step that
    /// needs Python finishes it with the GIL, and the region goes on with the next job. The
    /// region that ends the fetch also takes the issue order when the policy needs no Python:
    /// the ordered batch.
    fn fetch_released(
        &self,
        py: Python<'_>,
        ids: &[u64],
        issue: &IssueNow,
    ) -> PyResult<Option<Vec<InfRef>>> {
        let caps = self.caps(py)?;
        let plan = issue.native_order();
        if ids.is_empty() && (plan.is_none() || self.ready.borrow().is_empty()) {
            return Ok(None);
        }
        let mut i = 0;
        loop {
            let me = Unshared(self);
            let rest = Unshared(&ids[i..]);
            let caps_ref = Unshared(&caps);
            self.gate.enter();
            self.count_region(|r| r.regions += 1);
            let (out, ordered) = py
                .detach(move || {
                    let (me, rest, caps) = (me, rest, caps_ref);
                    for (k, jid) in rest.0.iter().enumerate() {
                        let Some(job) = me.0.job(*jid) else { continue };
                        if !me.0.fetchable(&job) {
                            continue;
                        }
                        let mut pass = Core::pass(&job);
                        match me.0.run_pass(None, &job, &mut pass, caps.0) {
                            Ok(Flow::Done(_)) => {}
                            Ok(Flow::NeedGil(_)) => {
                                return Unshared((Ok(Some((k, job, pass))), None))
                            }
                            Err(e) => return Unshared((Err((k, job, e)), None)),
                        }
                    }
                    Unshared((Ok(None), plan.and_then(|p| me.0.take_order(p))))
                })
                .0;
            self.gate.leave();
            self.flush(py)?;
            match out {
                Ok(None) => {
                    self.count_region(|r| match &ordered {
                        Some(_) => r.free_orders += 1,
                        None => r.gil_orders += 1,
                    });
                    return Ok(ordered);
                }
                Ok(Some((k, job, mut pass))) => {
                    match self.run_pass(Some(py), &job, &mut pass, &caps) {
                        Ok(_) => {}
                        Err(e) if is_exception(py, &e) => self.job_error(py, &job, &e)?,
                        Err(e) => return Err(e),
                    }
                    i += k + 1;
                }
                Err((k, job, e)) => {
                    if !is_exception(py, &e) {
                        return Err(e);
                    }
                    self.job_error(py, &job, &e)?;
                    i += k + 1;
                }
            }
            if i >= ids.len() {
                self.count_region(|r| r.gil_orders += 1);
                return Ok(None);
            }
        }
    }

    /// The ready batch in the issue policy's order, taken from ``ready``; None (the batch left in
    /// ``ready``) when the order needs Python.
    fn take_order(&self, plan: crate::runtime::builtins::Tag) -> Option<Vec<InfRef>> {
        let batch = std::mem::take(&mut *self.ready.borrow_mut());
        match self.order_native(plan, &batch) {
            Some(v) => Some(v),
            None => {
                *self.ready.borrow_mut() = batch;
                None
            }
        }
    }

    /// Retire for ``ids`` in order with the GIL released; a job whose retire meets a step that
    /// needs Python finishes it with the GIL, and the region goes on with the next job.
    fn retire_released(&self, py: Python<'_>, ids: &[u64]) -> PyResult<()> {
        let caps = self.retire_caps(py)?;
        let mut i = 0;
        while i < ids.len() {
            let me = Unshared(self);
            let rest = Unshared(&ids[i..]);
            let caps_ref = Unshared(&caps);
            self.gate.enter();
            self.count_region(|r| r.regions += 1);
            let out = py
                .detach(move || {
                    let (me, rest, caps) = (me, rest, caps_ref);
                    for (k, jid) in rest.0.iter().enumerate() {
                        let Some(job) = me.0.job(*jid) else { continue };
                        if job.borrow().ended {
                            continue;
                        }
                        let mut pass = RetirePass::new(&job);
                        match me.0.retire(None, &job, &mut pass, caps.0) {
                            Ok(Flow::Done(_)) => {}
                            Ok(Flow::NeedGil(_)) => return Unshared(Ok(Some((k, job, pass)))),
                            Err(e) => return Unshared(Err((k, job, e))),
                        }
                    }
                    Unshared(Ok(None))
                })
                .0;
            self.gate.leave();
            self.flush(py)?;
            match out {
                Ok(None) => break,
                Ok(Some((k, job, mut pass))) => {
                    match self.retire(Some(py), &job, &mut pass, &caps) {
                        Ok(_) => {}
                        Err(e) if is_exception(py, &e) => self.job_error(py, &job, &e)?,
                        Err(e) => return Err(e),
                    }
                    i += k + 1;
                }
                Err((k, job, e)) => {
                    if !is_exception(py, &e) {
                        return Err(e);
                    }
                    self.job_error(py, &job, &e)?;
                    i += k + 1;
                }
            }
        }
        Ok(())
    }

    fn add_phase(&self, phase: &'static str, dt: f64) {
        *self.stats.borrow_mut().phase_s.entry(phase).or_insert(0.0) += dt;
    }

    /// Drop registry entries of inflights no longer held, once they outnumber the live ones.
    fn sweep_owned(&self) {
        let n = self.owned.borrow().len();
        if n > 4096 && n > 2 * self.owned_live.get() {
            let mut o = self.owned.borrow_mut();
            o.retain(|_, w| w.strong_count() > 0);
            self.owned_live.set(o.len());
        }
    }

    // ------------------------------------------------------------ runs and stop
    /// Stop admitting; with ``drain=False`` cancel every resident job now.
    pub fn begin_stop(&self, py: Python<'_>, drain: bool) -> PyResult<()> {
        let shell = self.shell(py)?;
        shell.setattr(intern!(py, "stopping"), true)?;
        let run = shell.getattr(intern!(py, "run"))?;
        if !run.is_none() {
            run.setattr(intern!(py, "stopped"), true)?;
        }
        if !drain {
            let jobs: Vec<_> = self.jobs.borrow().values().cloned().collect();
            for job in jobs {
                self.cancel_job(py, &job, "the machine stopped")?;
            }
        }
        Ok(())
    }

    /// Run done, stop done, idle flag.
    pub fn after_cycle(&self, py: Python<'_>) -> PyResult<()> {
        let shell = self.shell(py)?;
        let settled = self.jobs.borrow().is_empty()
            && !shell.getattr(intern!(py, "sink_pending"))?.is_truthy()?;
        let idle = settled && shell.getattr(intern!(py, "source_buf"))?.is_none();
        let stopping = shell.getattr(intern!(py, "stopping"))?.is_truthy()?;
        let run = shell.getattr(intern!(py, "run"))?;
        if !run.is_none() && (idle || (stopping && settled)) {
            shell.call_method0(intern!(py, "release_source"))?;
            run.getattr(intern!(py, "done"))?
                .call_method0(intern!(py, "set"))?;
            shell.setattr(intern!(py, "run"), py.None())?;
        }
        if stopping && settled {
            shell.setattr(intern!(py, "running"), false)?;
        }
        let on_idle = shell.getattr(intern!(py, "on_idle"))?;
        if idle && self.inbox(py).get().is_empty() {
            on_idle.call_method0(intern!(py, "set"))?;
        } else {
            on_idle.call_method0(intern!(py, "clear"))?;
        }
        Ok(())
    }

    pub fn retire_caps(&self, py: Python<'_>) -> PyResult<RetireCaps> {
        Ok(RetireCaps {
            assert_alias: self.h.knob(py, K::AssertAlias)?.is_truthy()?,
            snapshot_every: self.h.knob_f64(py, K::SnapshotEvery)?,
            store: self.h.store.is_some(),
        })
    }
}
