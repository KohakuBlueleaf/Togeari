//! `each`: iterations at once (lazily started strands) or in order on one strand (spec/03 §3.5).

use std::cell::RefCell;
use std::rc::Rc;

use indexmap::IndexMap;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyInt, PyList};

use crate::program::core::{Instr, NameId};
use crate::runtime::core::Core;
use crate::runtime::events::{Exc, Stop};
use crate::runtime::frontend::fetch::Caps;
use crate::runtime::state::frame::{cut, pushed, Collector, CollectorRef};
use crate::runtime::state::{
    EachSpawn, Frame, FrameKind, Inflight, JobRef, Join, Key, NameMap, Resume, Slot, SlotRef,
    Strand, StrandRef,
};

fn item_slot(py: Python<'_>, items: &Py<PyList>, i: i64) -> PyResult<SlotRef> {
    Ok(Slot::constant(items.bind(py).get_item(i as usize)?.unbind()))
}

fn index_slot(py: Python<'_>, i: i64) -> SlotRef {
    Slot::constant(PyInt::new(py, i).into_any().unbind())
}

impl Core {
    fn bind_item(
        py: Python<'_>,
        st: &StrandRef,
        each: &Instr,
        items: &Py<PyList>,
        i: i64,
    ) -> PyResult<()> {
        let item = match each.item {
            Some(_) => Some(item_slot(py, items, i)?),
            None => None,
        };
        let mut s = st.borrow_mut();
        let names = s.names.as_mut().expect("a strand with names");
        if let (Some(name), Some(slot)) = (each.item, item) {
            names.set(name, slot);
        }
        if let Some(name) = each.index {
            names.set(name, index_slot(py, i));
        }
        Ok(())
    }

    pub fn op_each(
        &self,
        py: Python<'_>,
        job: &JobRef,
        st: &StrandRef,
        ins: &Rc<Instr>,
        caps: &Caps,
    ) -> Result<(), Stop> {
        let Some(v) = self.control_value(Some(py), job, st, ins, caps)? else {
            return Ok(());
        };
        let items = match py.get_type::<PyList>().call1((v.bind(py),)) {
            Ok(l) => l.cast_into::<PyList>().map_err(PyErr::from)?.unbind(),
            Err(e) if ins.collect_target.is_some() => {
                // Attribute snapshot failure to the original opening key, not its parked arm.
                {
                    let mut s = st.borrow_mut();
                    if s.resume == Resume::Parked {
                        let (prefix, counter) = s
                            .frames
                            .last()
                            .and_then(|f| f.saved.clone())
                            .expect("a parked frame");
                        s.prefix = prefix;
                        s.counter = counter - 1;
                        s.frames = cut(&s.frames, s.frames.len() - 1);
                        s.resume = Resume::No;
                    }
                }
                let key = st.borrow_mut().consume();
                let inf = Inflight::new(job, job.borrow().id, ins.clone(), key);
                {
                    let mut x = inf.borrow_mut();
                    x.control = true;
                    x.snapshot = Some(Core::snapshot_of(st));
                }
                self.end(Some(py), job, st)?;
                job.borrow_mut().add(&inf);
                self.fault(py, &inf, Exc::Obj(e.value(py).clone().into_any().unbind()), true)?;
                return Ok(());
            }
            Err(e) if e.is_instance_of::<PyTypeError>(py) => {
                let msg = e.value(py).str()?.to_string();
                return Err(Stop::Fault(format!("each: items are not iterable ({msg})")));
            }
            Err(e) => return Err(Stop::Err(e)),
        };

        // The `each`'s own key (taken when it parked, or now)
        let key = {
            let mut s = st.borrow_mut();
            let key = if s.resume == Resume::Parked {
                let (p, c) = s
                    .frames
                    .last()
                    .and_then(|f| f.saved.clone())
                    .expect("a parked frame");
                Key::of(&p, c - 1)
            } else {
                s.consume()
            };
            s.resume = Resume::No;
            key
        };

        let n = items.bind(py).len() as i64;
        let collector = if ins.collect_target.is_some() {
            let c = Rc::new(RefCell::new(Collector {
                ins: ins.clone(),
                key: key.clone(),
                count: n as usize,
                output: Slot::pending(),
                values: Vec::new(),
                slots: Vec::new(),
                snapshot: Some(Core::snapshot_of(st)),
                active: true,
                queued: false,
            }));
            job.borrow_mut().collectors.push(c.clone());
            self.checkpoint_extend_collectors(job);
            self.collect_finalize(py, job, &c)?;
            Some(c)
        } else {
            None
        };
        if n == 0 {
            if let Some(c) = &collector {
                st.borrow_mut()
                    .names
                    .as_mut()
                    .expect("names")
                    .set(ins.collect_target.expect("collect target"), c.borrow().output.clone());
            }
            st.borrow_mut().pc = ins.after.expect("an each's after address");
        } else if ins.has_exit || (job.borrow().cut.is_some() && collector.is_none()) {
            self.each_in_order(py, st, ins, Rc::new(items), key)?;
        } else {
            self.each_at_once(py, job, st, ins, Rc::new(items), n, key, collector)?;
        }
        Ok(())
    }

    pub fn collect_finalize(
        &self,
        py: Python<'_>,
        job: &JobRef,
        collector: &CollectorRef,
    ) -> PyResult<()> {
        let c = collector.borrow();
        if c.queued || !c.active {
            return Ok(());
        }
        let key = Key::of(&c.key.0, c.count as i64);
        let inf = Inflight::new(job, job.borrow().id, c.ins.clone(), key);
        {
            let mut x = inf.borrow_mut();
            x.control = true;
            x.collector = Some(collector.clone());
            x.snapshot = c.snapshot.clone();
        }
        drop(c);
        collector.borrow_mut().queued = true;
        job.borrow_mut().add(&inf);
        self.check(Some(py), &inf)
    }

    fn collect_capture(
        &self,
        py: Python<'_>,
        job: &JobRef,
        st: &StrandRef,
        frame: &Frame,
    ) -> Result<(), Stop> {
        let collector = frame.collector.as_ref().expect("collector");
        let ins = collector.borrow().ins.clone();
        let end = self
            .code
            .borrow()
            .instr(ins.iter_end.expect("end"))
            .expect("end instruction");
        let key = st.borrow_mut().consume();
        let inf = Inflight::new(job, job.borrow().id, end, key);
        let slot = match self.slot_of(Some(py), st, ins.collect_value.as_ref().expect("yield")) {
            Ok(slot) => slot,
            Err(Stop::Fault(_)) => self.undefined(),
            Err(error) => return Err(error),
        };
        {
            let mut x = inf.borrow_mut();
            x.control = true;
            x.collector = Some(collector.clone());
            x.collect_index = Some(frame.i as usize);
            x.snapshot = Some(Core::snapshot_of(st));
            x.inputs.push(slot);
        }
        job.borrow_mut().add(&inf);
        self.check(Some(py), &inf)?;
        Ok(())
    }

    // ------------------------------------------------------------ in order
    /// A body that can leave the loop: iterations follow the strand's program counter.
    fn each_in_order(
        &self,
        py: Python<'_>,
        st: &StrandRef,
        ins: &Rc<Instr>,
        items: Rc<Py<PyList>>,
        key: Key,
    ) -> PyResult<()> {
        {
            let mut s = st.borrow_mut();
            let f = Frame {
                reconv: ins.reconv,
                saved: Some((s.prefix.clone(), s.counter)),
                join: None,
                branch: Some(ins.addr),
                kind: FrameKind::Iter,
                each: Some(ins.addr),
                items: Some(items.clone()),
                i: 0,
                base: Some(key.clone()),
                collector: None,
            };
            s.frames = pushed(&s.frames, f);
            s.prefix = key.extend(0);
            s.counter = 0;
        }
        Core::bind_item(py, st, ins, &items, 0)?;
        st.borrow_mut().pc = ins.body.expect("an each body");
        Ok(())
    }

    pub fn op_iter_end(&self, py: Python<'_>, st: &StrandRef, ins: &Rc<Instr>) -> Result<(), Stop> {
        let f = st.borrow().frames.last().cloned();
        let f = match f {
            Some(f) if f.kind == FrameKind::Iter && f.each == ins.each_addr => f,
            _ => return Err(Stop::Fault("internal: iteration frame missing".to_string())),
        };
        let job = st.borrow().job.upgrade().expect("a resident job");
        if f.collector.is_some() {
            self.collect_capture(py, &job, st, &f)?;
            if let Some(join) = &f.join {
                self.resolve_join(Some(py), join, st)?;
                self.end(Some(py), &job, st)?;
                return Ok(());
            }
        } else {
            st.borrow_mut().consume();
        }
        let each = self
            .code
            .borrow()
            .instr(f.each.expect("an each address"))
            .expect("the each");
        let items = f.items.clone().expect("iteration items");

        // Next iteration, or leave the loop
        let i = f.i + 1;
        if i < items.bind(py).len() as i64 {
            {
                let mut s = st.borrow_mut();
                let n = s.frames.len() - 1;
                let mut next = f.copy();
                next.i = i;
                s.frames = pushed(&cut(&s.frames, n), next);
                s.prefix = f.base.as_ref().expect("an iteration base").extend(i);
                s.counter = 0;
            }
            Core::bind_item(py, st, &each, &items, i)?;
            st.borrow_mut().pc = each.body.expect("an each body");
        } else {
            let mut s = st.borrow_mut();
            let n = s.frames.len() - 1;
            s.frames = cut(&s.frames, n);
            let (p, c) = f.saved.clone().expect("a saved key");
            s.prefix = p;
            s.counter = c;
            if let Some(c) = &f.collector {
                s.names
                    .as_mut()
                    .expect("names")
                    .set(each.collect_target.expect("target"), c.borrow().output.clone());
            }
            s.pc = each.after.expect("an each's after address");
            drop(s);
            if let Some(c) = &f.collector {
                self.collect_finalize(py, &job, c)?;
            }
        }
        Ok(())
    }

    // ------------------------------------------------------------ at once
    /// One strand per item, started lazily in index order by a spawner strand; iteration i+1
    /// reads iteration i's carried names through phis, the strand continues after the loop.
    #[allow(clippy::too_many_arguments)]
    fn each_at_once(
        &self,
        py: Python<'_>,
        job: &JobRef,
        st: &StrandRef,
        ins: &Rc<Instr>,
        items: Rc<Py<PyList>>,
        n: i64,
        key: Key,
        collector: Option<CollectorRef>,
    ) -> PyResult<()> {
        let writes = &ins.body_may_write;
        let table = ins.body_writes.clone();
        let (base, saved, frames) = {
            let s = st.borrow();
            let base = s.names.as_ref().expect("a strand with names").snapshot();
            (base, (s.prefix.clone(), s.counter), s.frames.clone())
        };
        let jid = job.borrow().id;

        // The last iteration's join covers every body write (the names after the loop); phis
        // are made in name order
        let (last_names, carried): (Vec<NameId>, Vec<NameId>) = {
            let code = self.code.borrow();
            if writes.all {
                let mut all = base.keys();
                all.extend_from_slice(writes.ids());
                all.extend(ins.item);
                all.extend(ins.index);
                let sorted = code.names.sorted(all);
                (sorted.clone(), sorted)
            } else {
                let last = code.names.sorted(writes.ids().iter().copied());
                let carried = code.names.sorted(ins.carried.ids().iter().copied());
                (last, carried)
            }
        };
        let phis: IndexMap<NameId, SlotRef> =
            last_names.into_iter().map(|n| (n, Slot::phi())).collect();
        let last = Join::new(phis, table.clone(), writes.all);

        // A spawner starts the iterations as the window allows; it reads items and indexes as
        // shared values (no Python)
        let list = items.bind(py);
        let vals = list.iter().map(|x| Rc::new(x.unbind())).collect::<Vec<_>>();
        let idx = (0..n)
            .map(|i| Rc::new(PyInt::new(py, i).into_any().unbind()))
            .collect::<Vec<_>>();
        let e = EachSpawn {
            ins: ins.addr,
            items: items.clone(),
            vals,
            idx,
            n_items: n,
            key: key.clone(),
            base: base.clone(),
            saved,
            frames: frames.clone(),
            carried,
            table,
            last: last.clone(),
            prev: None,
            collector: collector.clone(),
        };
        let sp = Strand::new(
            job,
            jid,
            ins.addr,
            key.0.clone(),
            0,
            frames,
            None,
            Some(Rc::new(RefCell::new(e))),
        );
        job.borrow_mut().add_strand(&sp);

        // The strand itself continues after the loop
        let mut after = last.borrow().phis.clone();
        if let Some(c) = &collector {
            after.insert(ins.collect_target.expect("target"), c.borrow().output.clone());
        }
        if let Some(name) = ins.item {
            after.insert(name, item_slot(py, &items, n - 1)?);
        }
        if let Some(name) = ins.index {
            after.insert(name, index_slot(py, n - 1));
        }
        let mut s = st.borrow_mut();
        s.names = Some(NameMap::of(&base, Some(after)));
        s.pc = ins.after.expect("an each's after address");
        Ok(())
    }

    /// Start the next iteration strand; returns 1 (one step).
    pub fn spawn_next(
        &self,
        py: Option<Python<'_>>,
        job: &JobRef,
        sp: &StrandRef,
    ) -> PyResult<i64> {
        if let Some(cut) = &mut job.borrow_mut().cut {
            cut.extra_steps = cut.extra_steps.saturating_add(1);
        }
        let e = sp.borrow().spawn.clone().expect("a spawner");
        let i = sp.borrow().counter;
        let jid = job.borrow().id;
        let ins = self.code.borrow().instr(e.borrow().ins).expect("the each");

        // Names: those at the `each`, the previous iteration's carried phis, the item and index
        let (it, join) = {
            let es = e.borrow();
            let mut extra: IndexMap<NameId, SlotRef> = match &es.prev {
                Some(p) => p.borrow().phis.clone(),
                None => IndexMap::new(),
            };
            if let Some(name) = ins.item {
                extra.insert(name, Slot::shared(&es.vals[i as usize]));
            }
            if let Some(name) = ins.index {
                extra.insert(name, Slot::shared(&es.idx[i as usize]));
            }
            let join = if i == es.n_items - 1 {
                es.last.clone()
            } else {
                let phis = es.carried.iter().map(|n| (*n, Slot::phi())).collect();
                Join::new(phis, es.table.clone(), es.last.borrow().all_names)
            };

            // The iteration strand, ending at ITER_END with its join resolved
            let f = Frame {
                reconv: ins.iter_end,
                saved: Some(es.saved.clone()),
                join: Some(join.clone()),
                branch: Some(ins.addr),
                kind: FrameKind::Iter,
                each: Some(ins.addr),
                items: Some(es.items.clone()),
                i,
                base: Some(es.key.clone()),
                collector: es.collector.clone(),
            };
            let names = NameMap::of(&es.base, Some(extra));
            let body = ins.body.expect("an each body");
            let it = Strand::new(
                job,
                jid,
                body,
                es.key.extend(i),
                0,
                pushed(&es.frames, f),
                Some(names),
                None,
            );
            (it, join)
        };
        job.borrow_mut().add_strand(&it);
        e.borrow_mut().prev = Some(join);
        let done = {
            let mut s = sp.borrow_mut();
            s.counter = i + 1;
            s.counter == e.borrow().n_items
        };
        if done {
            self.end(py, job, sp)?;
        }
        Ok(1)
    }
}
