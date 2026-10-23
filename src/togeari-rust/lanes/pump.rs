//! Multi-issue of a native lane: every unit with free capacity takes work each pump
//! (`togeari.core.lanes.pump`).

use std::cmp::Ordering as Cmp;
use std::sync::atomic::{AtomicU64, Ordering};

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{IntoPyDict, PyDict, PyList, PyTuple};

use super::lane::{as_f64, ptr, py_max, py_min, sow_full, LaneCore, QSel, Submission, UState};
use super::policy::{AdmitsK, FormerNow, OrderK, SelectNow, TakeK};
use super::queue::{Num, Order, F};
use crate::contracts::hash::sha1;
use crate::runtime::builtins::plain_f64;
use crate::runtime::host::is_exception;
use crate::runtime::state::Obj;

// Queue waits kept between two `take_waits` calls
const WAITS_MAX: usize = 20_000;

// Timeout-heap size over the items in flight at which the heap is rebuilt
const TIMEOUT_SLACK: i64 = 256;

static SUB_IDS: AtomicU64 = AtomicU64::new(1);

/// A unit's capacity as Python counts it down: ``cap > 0``, then ``cap -= 1``.
enum Cap {
    I(i64),
    F(f64),
    Py(Obj),
}

impl Cap {
    fn of(v: &Bound<'_, PyAny>) -> Cap {
        if let Ok(i) = v.extract::<i64>() {
            if v.is_instance_of::<pyo3::types::PyInt>() {
                return Cap::I(i);
            }
        }
        match plain_f64(v) {
            Some(f) => Cap::F(f),
            None => Cap::Py(v.clone().unbind()),
        }
    }

    fn positive(&self, py: Python<'_>) -> PyResult<bool> {
        match self {
            Cap::I(i) => Ok(*i > 0),
            Cap::F(f) => Ok(*f > 0.0),
            Cap::Py(o) => o.bind(py).gt(0),
        }
    }

    fn dec(&mut self, py: Python<'_>) -> PyResult<()> {
        match self {
            Cap::I(i) => *i -= 1,
            Cap::F(f) => *f -= 1.0,
            Cap::Py(o) => *o = o.bind(py).sub(1)?.unbind(),
        }
        Ok(())
    }
}

/// How many items one submit carries from a bucket (taken while fewer, as Python's
/// ``len(taken) < want``), or which.
enum Want {
    N(f64),
    Items(Vec<Obj>),
}

/// Items of a unit's own queue it refused while taking (early forming), by item, once.
type Refused = Option<Vec<(usize, Obj)>>;

impl LaneCore {
    /// Submit to every unit up with free capacity; returns the items submitted.
    pub fn pump(&self, py: Python<'_>, now: f64) -> PyResult<usize> {
        {
            let mut st = self.st.borrow_mut();
            st.pump_needed = false;
            st.pumped_at = now;
        }
        let up = self.v_units_up(py)?;
        {
            let mut st = self.st.borrow_mut();

            // No unit up: the dead-lane clock runs, and its time counts as it passes
            if up.is_empty() {
                if st.no_unit_since.is_none() {
                    st.no_unit_since = Some(now);
                    st.no_unit_counted = now;
                }
                st.stats.no_unit_s += now - st.no_unit_counted;
                st.no_unit_counted = now;
                return Ok(0);
            }
            if st.no_unit_since.is_some() {
                st.stats.no_unit_s += now - st.no_unit_counted;
                st.no_unit_since = None;
            }
        }
        if !self.v_queued_now(py)?.truthy()? {
            return Ok(0);
        }
        let (early, shared) = {
            let st = self.st.borrow();
            (st.cfg.early, st.q.len() > 0)
        };
        let fm = self.former_now(py)?;
        let sel = self.select_now(py)?;

        // Early forming: shared-queue items go to unit queues first
        if early && shared {
            self.spread(py, fm.as_ref(), &sel, &up, now)?;
        }

        // Each unit in selector order, up to its capacity
        let mut sent = 0;
        let order = self.order(py, &sel, &up)?;
        for &u in &order {
            let qs = if early { QSel::Unit(u) } else { QSel::Shared };
            let queued = self
                .st
                .borrow_mut()
                .queue(qs)
                .is_some_and(|(q, _)| q.len() > 0);
            if !queued {
                continue;
            }
            self.promote(py, fm.as_ref(), qs, now)?;
            let Some(unit) = self.unit_obj(py, u) else {
                continue;
            };
            let mut cap = match unit.bind(py).call_method0(intern!(py, "capacity")) {
                Ok(c) => Cap::of(&c),
                Err(e) if is_exception(py, &e) => Cap::I(0),
                Err(e) => return Err(e),
            };

            // Early forming: items of the unit's own queue it no longer admits are noted
            let mut refused: Refused = early.then(Vec::new);
            loop {
                if !cap.positive(py)? || !self.v_under_cap(py)? {
                    break;
                }
                let up = self
                    .st
                    .borrow()
                    .state
                    .get(&u)
                    .is_some_and(|s| s.state == UState::Up);
                if !up {
                    break;
                }
                let items = self.take(py, fm.as_ref(), &sel, qs, u, now, &mut refused)?;
                if items.is_empty() {
                    break;
                }
                sent += items.len();
                self.submit(py, u, items, now)?;
                cap.dec(py)?;
            }
            if let Some(r) = refused.filter(|r| !r.is_empty()) {
                self.respread(py, fm.as_ref(), &sel, u, r, &order, now)?;
            }
        }
        Ok(sent)
    }

    pub fn select_now(&self, py: Python<'_>) -> PyResult<SelectNow> {
        let s = self.st.borrow().cfg.select.clone_ref(py);
        SelectNow::of(py, &self.builtins, s.bind(py))
    }

    // ------------------------------------------------------------ policies, contained
    /// The selector's unit order over the units up (each once); the given order when it raises.
    pub fn order(&self, py: Python<'_>, sel: &SelectNow, up: &[usize]) -> PyResult<Vec<usize>> {
        let r = self.order_raw(py, sel, up);
        self.contained(py, "selector order", r, up.to_vec())
    }

    fn up_objs(&self, py: Python<'_>, up: &[usize]) -> Vec<(usize, Obj)> {
        let st = self.st.borrow();
        up.iter()
            .filter_map(|u| st.state.get(u).map(|s| (*u, s.unit.clone_ref(py))))
            .collect()
    }

    fn order_raw(&self, py: Python<'_>, sel: &SelectNow, up: &[usize]) -> PyResult<Vec<usize>> {
        let units = self.up_objs(py, up);
        match &sel.order {
            OrderK::Given => Ok(up.to_vec()),

            // Most free capacity first, then the lowest reported waiting count
            OrderK::LeastLoaded => {
                let mut keys = Vec::with_capacity(units.len());
                for (_, u) in &units {
                    let ub = u.bind(py);
                    let waiting = match ub
                        .call_method0(intern!(py, "load"))
                        .and_then(|l| l.getattr(intern!(py, "waiting")))
                    {
                        Ok(w) => w,
                        Err(e) if is_exception(py, &e) => {
                            (1i64 << 30).into_pyobject(py)?.into_any()
                        }
                        Err(e) => return Err(e),
                    };
                    let cap = ub.call_method0(intern!(py, "capacity"))?;
                    keys.push((cap.neg()?, waiting));
                }
                sorted_by_keys(py, &units, keys)
            }

            // Units weighted by measured service rate
            OrderK::Throughput => {
                let mut keys = Vec::with_capacity(units.len());
                for (id, u) in &units {
                    let rate = self.v_neg_rate(py, *id)?;
                    let cap = u.bind(py).call_method0(intern!(py, "capacity"))?;
                    keys.push((rate, cap.neg()?));
                }
                sorted_by_keys(py, &units, keys)
            }

            // Units in turn: the starting unit moves by one each call
            OrderK::RoundRobin(s) => {
                if units.is_empty() {
                    return Ok(Vec::new());
                }
                let s = s.bind(py);
                let turn = s.getattr(intern!(py, "turn"))?.add(1)?.rem(units.len())?;
                s.setattr(intern!(py, "turn"), &turn)?;
                let t: usize = turn.extract()?;
                let ids: Vec<usize> = units.iter().map(|x| x.0).collect();
                Ok([&ids[t..], &ids[..t]].concat())
            }
            OrderK::User(s) => {
                let list = PyList::new(py, units.iter().map(|x| x.1.bind(py)))?;
                let got = s
                    .bind(py)
                    .call_method1(intern!(py, "order"), (list, self.lane(py)?))?;
                let mut out: Vec<usize> = Vec::new();
                for u in got.try_iter()? {
                    let p = ptr(&u?);
                    if up.contains(&p) && !out.contains(&p) {
                        out.push(p);
                    }
                }
                Ok(out)
            }
        }
    }

    /// The selector's admission of ``item`` to ``unit``; True when it raises.
    pub fn admits(
        &self,
        py: Python<'_>,
        sel: &SelectNow,
        item: &Obj,
        unit: usize,
    ) -> PyResult<bool> {
        let r = match &sel.admits {
            AdmitsK::All => return Ok(true),
            AdmitsK::ByHint => self.by_hint(py, item, unit),
            AdmitsK::Affinity(s) => self.affinity(py, s, item, unit),
            AdmitsK::User(s) => match self.unit_obj(py, unit) {
                Some(u) => s
                    .bind(py)
                    .call_method1(
                        intern!(py, "admits"),
                        (item.bind(py), u.bind(py), self.lane(py)?),
                    )
                    .and_then(|v| v.is_truthy()),
                None => Ok(true),
            },
        };
        self.contained(py, "selector admits", r, true)
    }

    fn up_names<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let up = self.v_units_up(py)?;
        let mut out = Vec::new();
        for (_, u) in self.up_objs(py, &up) {
            out.push(u.bind(py).getattr(intern!(py, "name"))?);
        }
        Ok(out)
    }

    /// The unit named by ``@hint unit=``; anything else, or a name no unit up has, goes anywhere.
    fn by_hint(&self, py: Python<'_>, item: &Obj, unit: usize) -> PyResult<bool> {
        let ext = item.bind(py).getattr(intern!(py, "ext"))?;
        let hint = ext.call_method1(intern!(py, "get"), ("hint", PyDict::new(py)))?;
        let want = hint.call_method1(intern!(py, "get"), ("unit",))?;
        if want.is_none() {
            return Ok(true);
        }
        let Some(u) = self.unit_obj(py, unit) else {
            return Ok(true);
        };
        if want.eq(u.bind(py).getattr(intern!(py, "name"))?)? {
            return Ok(true);
        }
        for n in self.up_names(py)? {
            if !n.ne(&want)? {
                return Ok(false);
            }
        }
        Ok(true)
    }

    /// Affinity: the preferred unit (rendezvous hash of the item's key over the units up,
    /// remembered in the selector's ``_pref`` while the units up are unchanged) admits, any
    /// unit while the preferred one has no capacity.
    fn affinity(&self, py: Python<'_>, s: &Obj, item: &Obj, unit: usize) -> PyResult<bool> {
        let Some(pref) = self.preferred(py, s, item)? else {
            return Ok(true);
        };
        if pref.bind(py).as_ptr() as usize == unit {
            return Ok(true);
        }
        pref.bind(py).call_method0(intern!(py, "capacity"))?.le(0)
    }

    fn preferred(&self, py: Python<'_>, s: &Obj, item: &Obj) -> PyResult<Option<Obj>> {
        let up = self.v_units_up(py)?;
        let units = self.up_objs(py, &up);
        if units.is_empty() {
            return Ok(None);
        }
        let s = s.bind(py);
        let it = item.bind(py);

        // The item's key: its job, a function of it, or one of its inputs
        let key = s.getattr(intern!(py, "key"))?;
        let ik = if key.eq("job")? {
            it.getattr(intern!(py, "ext"))?
                .call_method1(intern!(py, "get"), ("job_key",))?
        } else if key.is_callable() {
            key.call1((it,))?
        } else {
            it.getattr(intern!(py, "inputs"))?
                .call_method1(intern!(py, "get"), (&key,))?
        };

        // Remembered per (lane, key) while the lane's units up are unchanged
        let lane_id = self.lane(py)?.as_ptr() as usize;
        let k = PyTuple::new(py, [lane_id.into_pyobject(py)?.into_any(), ik.clone()])?;
        let memo = s.getattr(intern!(py, "_pref"))?;
        let hit = memo.call_method1(intern!(py, "get"), (&k,))?;
        let version = self.st.borrow().up_version;
        if !hit.is_none() && hit.get_item(0)?.eq(version)? {
            return Ok(Some(hit.get_item(1)?.unbind()));
        }
        let limit = self
            .select_mod
            .bind(py)
            .getattr(intern!(py, "_PREF_LIMIT"))?;
        if memo.len()?.into_pyobject(py)?.ge(&limit)? {
            memo.call_method0(intern!(py, "clear"))?;
        }

        // Rendezvous: the unit with the largest sha1 of "{key!r}/{name}", the first on a tie
        let r = ik.repr()?.to_string();
        let mut best: Option<([u8; 20], &Obj)> = None;
        for (_, u) in &units {
            let name = u.bind(py).getattr(intern!(py, "name"))?.str()?.to_string();
            let d = sha1(format!("{r}/{name}").as_bytes());
            if best.as_ref().is_none_or(|(b, _)| d > *b) {
                best = Some((d, u));
            }
        }
        let unit = best.expect("a unit up").1.clone_ref(py);
        let entry =
            PyTuple::new(py, [version.into_pyobject(py)?.into_any(), unit.bind(py).clone()])?;
        memo.set_item(&k, entry)?;
        Ok(Some(unit))
    }

    /// How many items one submit to ``unit`` carries from bucket ``b``, or which; the batch
    /// limit when the former raises.
    fn count(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        qs: QSel,
        unit: usize,
        b: u64,
        now: f64,
    ) -> PyResult<Want> {
        let Some(u) = self.unit_obj(py, unit) else {
            return Ok(Want::N(0.0));
        };
        let ub = u.bind(py);
        let batch = match ub.getattr_opt(intern!(py, "kind"))? {
            Some(k) => k.eq("batch")?,
            None => false,
        };
        if !batch {
            return Ok(Want::N(1.0));
        }
        let mb = ub.getattr_opt(intern!(py, "max_batch"))?;
        let limit = match &mb {
            Some(v) if v.is_truthy()? => py_max(1.0, as_f64(v)?),
            _ => 1.0,
        };
        let Some(fm) = fm else {
            return Ok(Want::N(limit));
        };
        match &fm.take {
            TakeK::Nothing => Ok(Want::N(0.0)),
            TakeK::Greedy => Ok(Want::N(limit)),
            TakeK::Sow { size } => {
                let (n, due, cap) = {
                    let mut st = self.st.borrow_mut();
                    match st.queue(qs) {
                        Some((q, _)) => (q.bucket_len(b), q.due(b), q.cap),
                        None => return Ok(Want::N(0.0)),
                    }
                };
                let full = sow_full(n, *size, cap);
                if !(full || due.is_some_and(|d| d <= now)) {
                    return Ok(Want::N(0.0));
                }
                let mine = match &mb {
                    Some(v) if v.is_truthy()? => as_f64(v)?,
                    _ => *size,
                };
                let want = py_max(1.0, py_min(*size, mine));
                Ok(Want::N(py_min(want, limit)))
            }
            TakeK::User(f) => {
                let view = self.view(py, qs, b)?;
                let r = f
                    .bind(py)
                    .call_method1(intern!(py, "take"), (view, ub, now))
                    .map(|w| w.unbind());
                let Some(got) = self.contained(py, "former take", r.map(Some), None)? else {
                    return Ok(Want::N(limit));
                };

                // ``want[:limit]`` or ``min(want or 0, limit)``, with the limit as Python has it
                let got = got.bind(py);
                let builtins = py.import("builtins")?;
                let lim: Bound<'_, PyAny> = match &mb {
                    Some(v) if v.is_truthy()? => {
                        builtins.getattr(intern!(py, "max"))?.call1((1, v))?
                    }
                    _ => 1i64.into_pyobject(py)?.into_any(),
                };
                if got.is_instance_of::<PyList>() {
                    let cut = builtins
                        .getattr(intern!(py, "slice"))?
                        .call1((py.None(), &lim))?;
                    let l = got.get_item(cut)?;
                    return Ok(Want::Items(
                        l.try_iter()?
                            .map(|x| x.map(|x| x.unbind()))
                            .collect::<PyResult<_>>()?,
                    ));
                }
                if !got.is_truthy()? {
                    return Ok(Want::N(0.0));
                }
                let m = builtins.getattr(intern!(py, "min"))?.call1((got, &lim))?;
                Ok(Want::N(as_f64(&m)?))
            }
        }
    }

    // ------------------------------------------------------------ taking
    /// Items for ``unit`` from the first ripe bucket (by head order) holding items it admits.
    #[allow(clippy::too_many_arguments)]
    fn take(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        sel: &SelectNow,
        qs: QSel,
        unit: usize,
        now: f64,
        refused: &mut Refused,
    ) -> PyResult<Vec<Obj>> {
        let mut aside = Vec::new();
        let mut out = Vec::new();
        loop {
            let b = self
                .st
                .borrow_mut()
                .queue(qs)
                .and_then(|(q, _)| q.ripe_top());
            let Some(b) = b else { break };

            // The bucket's batch, then its ripeness again
            out = match self.count(py, fm, qs, unit, b, now)? {
                Want::N(n) if n > 0.0 => self.pop_count(py, sel, qs, unit, b, n, refused)?,
                Want::Items(items) if !items.is_empty() => {
                    self.pop_named(py, sel, qs, unit, b, items, refused)?
                }
                _ => Vec::new(),
            };
            if !out.is_empty() {
                if let Some((q, _)) = self.st.borrow_mut().queue(qs) {
                    q.after_take(b);
                }
                self.refresh(py, fm, qs, b, now)?;
                break;
            }
            let x = self
                .st
                .borrow_mut()
                .queue(qs)
                .and_then(|(q, _)| q.ripe_pop());
            aside.extend(x);
        }
        if let Some((q, _)) = self.st.borrow_mut().queue(qs) {
            for x in aside {
                q.ripe_push(x);
            }
            q.ripe_compact();
        }
        Ok(out)
    }

    /// ``admits``, noting a refusal of an item of the unit's own queue (early forming).
    fn admits_noting(
        &self,
        py: Python<'_>,
        sel: &SelectNow,
        item: &Obj,
        unit: usize,
        refused: &mut Refused,
    ) -> PyResult<bool> {
        let ok = self.admits(py, sel, item, unit)?;
        if let (false, Some(r)) = (ok, refused.as_mut()) {
            let p = item.as_ptr() as usize;
            if !r.iter().any(|(q, _)| *q == p) {
                r.push((p, item.clone_ref(py)));
            }
        }
        Ok(ok)
    }

    /// The first ``n`` admitted live items in queue order (while fewer than ``n``); the rest go
    /// back.
    #[allow(clippy::too_many_arguments)]
    fn pop_count(
        &self,
        py: Python<'_>,
        sel: &SelectNow,
        qs: QSel,
        unit: usize,
        b: u64,
        n: f64,
        refused: &mut Refused,
    ) -> PyResult<Vec<Obj>> {
        let mut taken: Vec<u64> = Vec::new();
        let mut aside: Vec<(Order, u64)> = Vec::new();
        while (taken.len() as f64) < n {
            let next = {
                let mut st = self.st.borrow_mut();
                st.queue(qs).and_then(|(q, _)| {
                    q.pop_live(b)
                        .and_then(|x| q.item_of(py, x.1).map(|i| (x, i)))
                })
            };
            let Some((x, item)) = next else { break };
            if self.admits_noting(py, sel, &item, unit, refused)? {
                taken.push(x.1);
            } else {
                aside.push(x);
            }
        }
        let mut st = self.st.borrow_mut();
        let Some((q, _)) = st.queue(qs) else {
            return Ok(Vec::new());
        };
        for x in aside {
            q.push_back(b, x);
        }
        Ok(q.take_entries(py, &taken))
    }

    /// The admitted items a former named; each once, and only items queued in this bucket.
    #[allow(clippy::too_many_arguments)]
    fn pop_named(
        &self,
        py: Python<'_>,
        sel: &SelectNow,
        qs: QSel,
        unit: usize,
        b: u64,
        want: Vec<Obj>,
        refused: &mut Refused,
    ) -> PyResult<Vec<Obj>> {
        let mut taken: Vec<u64> = Vec::new();
        let mut seen: Vec<usize> = Vec::new();
        let mut bad = 0;
        for it in want {
            let p = it.as_ptr() as usize;
            let e = self
                .st
                .borrow_mut()
                .queue(qs)
                .and_then(|(q, _)| q.entry_in(p, b));
            let Some(e) = e.filter(|_| !seen.contains(&p)) else {
                bad += 1;
                continue;
            };
            seen.push(p);
            if self.admits_noting(py, sel, &it, unit, refused)? {
                taken.push(e);
            }
        }
        if bad > 0 {
            let msg = format!("{bad} named item(s) repeated or not in the bucket; skipped");
            self.policy_failed(py, "former take", &pyo3::exceptions::PyValueError::new_err(msg));
        }
        let mut st = self.st.borrow_mut();
        Ok(st
            .queue(qs)
            .map(|(q, _)| q.take_entries(py, &taken))
            .unwrap_or_default())
    }

    // ------------------------------------------------------------ routing
    /// Shared-queue items to unit queues: the first unit in selector order admitting each.
    fn spread(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        sel: &SelectNow,
        up: &[usize],
        now: f64,
    ) -> PyResult<()> {
        let mut order = self.order(py, sel, up)?;
        if order.is_empty() {
            order = up.to_vec();
        }
        let items = self.drain(py, QSel::Shared)?;
        for (o, item) in items {
            let unit = self.first_admitting(py, sel, &item, &order)?;
            self.push(py, fm, QSel::Unit(unit), item, o, now)?;
        }
        Ok(())
    }

    fn first_admitting(
        &self,
        py: Python<'_>,
        sel: &SelectNow,
        item: &Obj,
        cands: &[usize],
    ) -> PyResult<usize> {
        for u in cands {
            if self.admits(py, sel, item, *u)? {
                return Ok(*u);
            }
        }
        Ok(cands[0])
    }

    /// Refused items still in ``unit``'s queue move, with their order key, to the first other
    /// unit in ``order`` that admits them; an item no other unit admits stays.
    #[allow(clippy::too_many_arguments)]
    fn respread(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        sel: &SelectNow,
        unit: usize,
        refused: Vec<(usize, Obj)>,
        order: &[usize],
        now: f64,
    ) -> PyResult<()> {
        let qs = QSel::Unit(unit);
        for (p, item) in refused {
            let at = self
                .st
                .borrow_mut()
                .queue(qs)
                .and_then(|(q, _)| q.order_of(p));
            let Some(o) = at else { continue };
            let mut to = None;
            for u in order.iter().filter(|u| **u != unit) {
                if self.admits(py, sel, &item, *u)? {
                    to = Some(*u);
                    break;
                }
            }
            let Some(to) = to else { continue };
            self.remove_from(py, fm, qs, p, now)?;
            self.push(py, fm, QSel::Unit(to), item, o, now)?;
            self.st.borrow_mut().pump_needed = true;
        }
        Ok(())
    }

    /// Queue ``item``: a unit queue under early forming with a unit up, else the shared one.
    pub fn route(
        &self,
        py: Python<'_>,
        fm: Option<&FormerNow>,
        item: Obj,
        order: Order,
        now: f64,
    ) -> PyResult<()> {
        self.st.borrow_mut().pump_needed = true;
        let up = self.v_units_up(py)?;
        let early = self.st.borrow().cfg.early;
        if early && !up.is_empty() {
            let sel = self.select_now(py)?;
            let mut cands = self.order(py, &sel, &up)?;
            if cands.is_empty() {
                cands = up.clone();
            }
            let unit = self.first_admitting(py, &sel, &item, &cands)?;
            return self.push(py, fm, QSel::Unit(unit), item, order, now);
        }
        self.push(py, fm, QSel::Shared, item, order, now)
    }

    // ------------------------------------------------------------ submissions
    fn submit(&self, py: Python<'_>, unit: usize, items: Vec<Obj>, now: f64) -> PyResult<()> {
        let lane = self.lane(py)?;
        let Some(unit_obj) = self.unit_obj(py, unit) else {
            return Ok(());
        };
        let ub = unit_obj.bind(py);

        // Expired call caps wait only for timer settlement, never engine submission.
        let mut live = Vec::with_capacity(items.len());
        for item in items {
            let cap = item
                .bind(py)
                .getattr(intern!(py, "ext"))?
                .call_method1(intern!(py, "get"), ("deadline_cap",))?;
            if !cap.is_none() && now >= as_f64(&cap)? {
                let mut st = self.st.borrow_mut();
                let seq = st.next_seq();
                st.held_index.insert(item.as_ptr() as usize, seq);
                st.held_items.insert(seq, item);
            } else {
                live.push(item);
            }
        }
        if live.is_empty() {
            return Ok(());
        }

        // Plugins see (and may block) items after unit choice
        let mut items = live;
        let mut now = now;
        let hook = lane.getattr(intern!(py, "hook_submit"))?;
        if !hook.is_none() {
            let list = PyList::new(py, items.iter().map(|i| i.bind(py)))?;
            let got = hook.call1((&lane, ub, list))?;
            if !got.is_truthy()? {
                return Ok(());
            }
            items = got
                .try_iter()?
                .map(|x| x.map(|x| x.unbind()))
                .collect::<PyResult<Vec<_>>>()?;
            // Preparation uses the call budget; each attempt starts after the hook.
            now = now.max(self.now(py)?);
            let mut live = Vec::with_capacity(items.len());
            for item in items {
                let cap = item
                    .bind(py)
                    .getattr(intern!(py, "ext"))?
                    .call_method1(intern!(py, "get"), ("deadline_cap",))?;
                if !cap.is_none() && now >= as_f64(&cap)? {
                    let mut st = self.st.borrow_mut();
                    let seq = st.next_seq();
                    st.held_index.insert(item.as_ptr() as usize, seq);
                    st.held_items.insert(seq, item);
                } else {
                    live.push(item);
                }
            }
            items = live;
            if items.is_empty() {
                return Ok(());
            }
        }

        // Submission and in-flight accounting
        let sid = SUB_IDS.fetch_add(1, Ordering::Relaxed);
        let n = items.len();
        {
            let mut st = self.st.borrow_mut();
            st.subs.insert(
                sid,
                Submission {
                    unit,
                    unit_obj: unit_obj.clone_ref(py),
                    items: items.iter().map(|i| i.clone_ref(py)).collect(),
                    ticket: None,
                    sent: now,
                },
            );
            st.inflight += n as i64;
            if let Some(us) = st.state.get_mut(&unit) {
                us.subs.insert(sid);
                us.inflight += n as i64;
                if us.busy_since.is_none() {
                    us.busy_since = Some(now);
                }
            }
        }

        // Per item: protocol, submission, queue wait, timeout
        let protocol = ub.getattr_opt(intern!(py, "protocol"))?;
        let name = ub.getattr(intern!(py, "name"))?;
        for it in &items {
            let itb = it.bind(py);
            if let Some(p) = protocol.as_ref().filter(|p| p.is_truthy().unwrap_or(false)) {
                itb.setattr(intern!(py, "protocol"), p)?;
            }
            let ext = itb.getattr(intern!(py, "ext"))?;
            ext.set_item("sub", sid)?;
            ext.set_item("unit", &name)?;
            ext.set_item("sent", now)?;
            let enq = as_f64(&ext.call_method1(intern!(py, "get"), ("enqueued", now))?)?;
            self.st.borrow_mut().waits.push(now - enq);
            self.arm_timeout(py, it, sid, now)?;
        }
        let batch = match ub.getattr_opt(intern!(py, "kind"))? {
            Some(k) => k.eq("batch")?,
            None => false,
        };
        {
            let mut st = self.st.borrow_mut();
            if st.waits.len() > WAITS_MAX {
                st.waits.drain(..WAITS_MAX / 2);
            }

            // Counters
            st.stats.submitted += n as i64;
            if n > 1 || batch {
                st.stats.batches += 1;
                *st.batch_sizes.entry(n as i64).or_insert(0) += 1;
            }
        }

        // Hand the work to the unit; a submit that raises answers every item with UnitError
        let done = Py::new(py, super::python::Done::new(lane.clone().unbind(), sid))?;
        let work: Bound<'_, PyAny> = if batch {
            PyList::new(py, items.iter().map(|i| i.bind(py)))?.into_any()
        } else {
            items[0].bind(py).clone()
        };
        let t0 = std::time::Instant::now();
        match ub.call_method1(intern!(py, "submit"), (work, done)) {
            Ok(t) => {
                if let Some(s) = self.st.borrow_mut().subs.get_mut(&sid) {
                    s.ticket = Some(t.unbind());
                }
            }
            Err(e) if is_exception(py, &e) => {
                let err = self.rc.unit_error.bind(py).call1((e.value(py),))?;
                let res = PyList::new(py, std::iter::repeat_n(err, n))?;
                lane.getattr(intern!(py, "on_done"))?
                    .call1((&lane, sid, res))?;
            }
            Err(e) => return Err(e),
        }
        let lat = t0.elapsed().as_secs_f64();
        if let Some(us) = self.st.borrow_mut().state.get_mut(&unit) {
            us.submit_lat = lat;
        }

        // The event carries the first item's job, so job sampling applies to it, and each
        // item's (job key, address, order key)
        let jid = items[0]
            .bind(py)
            .getattr(intern!(py, "ext"))?
            .call_method1(intern!(py, "get"), ("job",))?;
        if lane
            .getattr(intern!(py, "wants"))?
            .call1(("lane.submit", &jid))?
            .is_truthy()?
        {
            let work = PyList::empty(py);
            for it in &items {
                work.append(place(py, it.bind(py))?)?;
            }
            let kw = PyDict::new(py);
            kw.set_item("job", jid)?;
            kw.set_item("lane", lane.getattr(intern!(py, "name"))?)?;
            kw.set_item("unit", ub.getattr(intern!(py, "name"))?)?;
            kw.set_item("items", n)?;
            kw.set_item("work", work)?;
            lane.getattr(intern!(py, "emit"))?
                .call(("lane.submit",), Some(&kw))?;
        }
        Ok(())
    }

    /// The item's deadline in flight: ``sent + timeout_s``, or the ``WorkItem.deadline`` it
    /// carried when queued, whichever is earlier.
    fn arm_timeout(&self, py: Python<'_>, item: &Obj, sid: u64, now: f64) -> PyResult<()> {
        let itb = item.bind(py);
        let timeout = self.st.borrow().cfg.timeout_s;
        let deadline = self
            .deadlines
            .bind(py)
            .call_method1("arm", (itb, now, timeout))?;
        if deadline.is_none() {
            return Ok(());
        }
        let deadline = as_f64(&deadline)?;
        let rebuild = {
            let mut st = self.st.borrow_mut();
            let s = st.next_seq();
            st.timeouts
                .push(std::cmp::Reverse((F(deadline), s, Some(sid))));
            st.timeout_items.insert(s, item.clone_ref(py));
            st.timeouts.len() as i64 > 2 * st.inflight + TIMEOUT_SLACK
        };

        // Entries of settled items are dropped once they outnumber the items in flight
        if rebuild {
            let entries = std::mem::take(&mut self.st.borrow_mut().timeouts).into_vec();
            let mut keep = Vec::with_capacity(entries.len());
            for e in entries {
                let std::cmp::Reverse((_, s, sub)) = e;
                if self.timing(py, s, sub)? {
                    keep.push(e);
                } else {
                    self.st.borrow_mut().timeout_items.remove(&s);
                }
            }
            let mut st = self.st.borrow_mut();
            let added = std::mem::take(&mut st.timeouts);
            st.timeouts = keep.into();
            st.timeouts.extend(added);
        }
        Ok(())
    }

    /// Close a submission: release in-flight counts and busy time; its unit, items and unit
    /// object.
    pub fn close(&self, sid: u64, now: f64) -> (usize, Vec<Obj>, Obj) {
        let mut st = self.st.borrow_mut();
        let sub = st.subs.remove(&sid).expect("an open submission");
        let n = sub.items.len() as i64;
        st.inflight -= n;
        if let Some(us) = st.state.get_mut(&sub.unit) {
            us.subs.remove(&sid);
            us.inflight -= n;
            if us.subs.is_empty() {
                if let Some(since) = us.busy_since.take() {
                    us.busy_total += now - since;
                }
            }
        }
        (sub.unit, sub.items, sub.unit_obj)
    }

    /// The items and unit of an open submission.
    pub fn sub_items(&self, py: Python<'_>, sid: u64) -> Option<(Vec<Obj>, Obj)> {
        let st = self.st.borrow();
        st.subs
            .get(&sid)
            .map(|s| (s.items.iter().map(|i| i.clone_ref(py)).collect(), s.unit_obj.clone_ref(py)))
    }
}

/// The unit order by sort keys computed per unit (Python's stable ``sorted``): natively when
/// every key part is a plain number, else by Python's own comparisons.
fn sorted_by_keys<'py>(
    py: Python<'py>,
    units: &[(usize, Obj)],
    keys: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>,
) -> PyResult<Vec<usize>> {
    let plain = |v: &Bound<'py, PyAny>| -> Option<Num> {
        if v.is_instance_of::<pyo3::types::PyInt>() {
            v.extract::<i128>().ok().map(Num::I)
        } else {
            plain_f64(v).filter(|f| !f.is_nan()).map(Num::F)
        }
    };
    let nums: Option<Vec<(Num, Num)>> = keys
        .iter()
        .map(|(a, b)| Some((plain(a)?, plain(b)?)))
        .collect();
    if let Some(nums) = nums {
        let mut idx: Vec<usize> = (0..units.len()).collect();
        idx.sort_by(|i, j| nums[*i].partial_cmp(&nums[*j]).unwrap_or(Cmp::Equal));
        return Ok(idx.into_iter().map(|i| units[i].0).collect());
    }
    let tuples = PyList::empty(py);
    for (a, b) in keys {
        tuples.append(PyTuple::new(py, [a, b])?)?;
    }
    let order = py
        .import("builtins")?
        .getattr(intern!(py, "sorted"))?
        .call(
            (pyo3::types::PyRange::new(py, 0, units.len() as isize)?,),
            Some(&[("key", tuples.getattr(intern!(py, "__getitem__"))?)].into_py_dict(py)?),
        )?;
    let mut out = Vec::with_capacity(units.len());
    for i in order.try_iter()? {
        out.push(units[i?.extract::<usize>()?].0);
    }
    Ok(out)
}

/// (job key, address, order key) of an item a statement owns; None parts otherwise.
fn place<'py>(py: Python<'py>, item: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
    let ext = item.getattr(intern!(py, "ext"))?;
    let job_key = ext.call_method1(intern!(py, "get"), ("job_key",))?;
    let owner = item.getattr(intern!(py, "owner"))?;
    let instr = owner.getattr_opt(intern!(py, "instr"))?;
    let addr = match instr {
        Some(i) if !i.is_none() => i
            .getattr_opt(intern!(py, "addr"))?
            .unwrap_or_else(|| py.None().into_bound(py)),
        _ => py.None().into_bound(py),
    };
    let key = owner
        .getattr_opt(intern!(py, "key"))?
        .unwrap_or_else(|| py.None().into_bound(py));
    PyTuple::new(py, [job_key, addr, key])
}
