//! Bounded metadata-only wait evidence. No policy callbacks or heap cleanup.
use crate::program::core::Operand;
use crate::runtime::core::Core;
use crate::runtime::state::{InfState, Key, SlotRef, SlotState, StrandState};
use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::{HashMap, HashSet};

struct Budget {
    left: usize,
    truncated: bool,
}
impl Budget {
    fn take(&mut self) -> bool {
        if self.left == 0 {
            self.truncated = true;
            false
        } else {
            self.left -= 1;
            true
        }
    }
}
fn slot_at(slot: &SlotRef, key: &Key, budget: &mut Budget) -> Option<SlotRef> {
    let mut slot = slot.clone();
    let mut seen = HashSet::new();
    loop {
        if !budget.take() || !seen.insert(slot.borrow().id) {
            return None;
        }
        let next = {
            let s = slot.borrow();
            if let Some(next) = &s.forward {
                Some(next.clone())
            } else {
                let mut best: Option<(&Key, &SlotRef)> = None;
                for (k, v) in &s.muts {
                    if !budget.take() {
                        return None;
                    }
                    if k < key
                        && v.borrow().state != SlotState::Squashed
                        && best.is_none_or(|(old, _)| k > old)
                    {
                        best = Some((k, v));
                    }
                }
                best.map(|(_, v)| v.clone())
            }
        };
        match next {
            Some(next) => slot = next,
            None => return Some(slot),
        }
    }
}
impl Core {
    pub(crate) fn wait_state(
        &self,
        py: Python<'_>,
        selected: Option<u64>,
        limit: usize,
    ) -> PyResult<Py<PyDict>> {
        if let Some(id) = selected {
            if !self.jobs.borrow().contains_key(&id) {
                return Err(pyo3::exceptions::PyKeyError::new_err(format!("no resident job {id}")));
            }
        }
        let mut budget = Budget { left: limit, truncated: false };
        let out = PyDict::new(py);
        let jobs_out = PyList::empty(py);
        let unsupported = PyList::empty(py);
        if limit == 0 && !self.jobs.borrow().is_empty() {
            budget.truncated = true;
        }
        let jobs: Vec<_> = self
            .jobs
            .borrow()
            .iter()
            .filter(|(id, _)| selected.is_none_or(|chosen| chosen == **id))
            .take(limit)
            .map(|(_, job)| job.clone())
            .collect();
        for job in jobs {
            if !budget.take() {
                break;
            }
            let j = job.borrow();
            let row = PyDict::new(py);
            row.set_item("id", j.id)?;
            row.set_item("key", j.key.bind(py))?;
            row.set_item("job_execution_id", &j.job_execution_id)?;
            row.set_item("paused", j.paused)?;
            row.set_item("unfinished", j.unfinished)?;
            row.set_item("window_size", j.window.len())?;
            let inflights = PyList::empty(py);
            let strands = PyList::empty(py);
            let mut slots: IndexMap<u64, SlotRef> = IndexMap::new();
            let mut producers: HashMap<u64, Vec<u64>> = HashMap::new();
            let mut owners: HashMap<u64, Vec<u64>> = HashMap::new();
            let infs: Vec<_> = j.window.live().iter().take(budget.left).cloned().collect();
            let live: Vec<_> = j.strands.values().take(budget.left).cloned().collect();
            if infs.len() < j.window.len() || live.len() < j.strands.len() {
                budget.truncated = true;
            }
            let oldest_live = live
                .iter()
                .filter_map(|st| {
                    let st = st.borrow();
                    (st.state != StrandState::Ended).then(|| st.next_key())
                })
                .min();
            let window_limit = self
                .h
                .knobs
                .bind(py)
                .get_item("window")?
                .and_then(|v| v.extract::<f64>().ok());
            let retire_limit = self
                .h
                .knobs
                .bind(py)
                .get_item("retire_buffer")?
                .and_then(|v| v.extract::<f64>().ok());
            let mutation_wait = self
                .h
                .knobs
                .bind(py)
                .get_item("mutates")?
                .and_then(|v| v.extract::<String>().ok())
                .is_some_and(|s| s == "wait");
            for inf in &infs {
                if !budget.take() {
                    break;
                }
                let inf = inf.borrow();
                let data = PyDict::new(py);
                data.set_item("id", inf.id)?;
                data.set_item("addr", inf.ins.addr)?;
                data.set_item("key", inf.key.to_py(py))?;
                data.set_item("state", inf.state.name())?;
                data.set_item("invocation_id", inf.invocation_id.as_deref())?;
                let attempts = PyList::empty(py);
                for attempt in &inf.attempt_ids {
                    if !budget.take() {
                        break;
                    }
                    attempts.append(attempt)?;
                }
                data.set_item("attempt_ids", attempts)?;
                data.set_item("fault", inf.fault.is_some())?;
                data.set_item("resolution", inf.resolution)?;
                data.set_item("item", inf.item.as_ref().map(|item| item.bind(py)))?;
                let inputs = PyList::empty(py);
                let outputs = PyList::empty(py);
                let blockers = PyList::empty(py);
                for ((port, _), slot) in inf.ins.args.iter().zip(&inf.inputs) {
                    if !budget.take() {
                        break;
                    }
                    if let Some(slot) = slot_at(slot, &inf.key, &mut budget) {
                        let id = slot.borrow().id;
                        slots.entry(id).or_insert(slot);
                        let input = PyDict::new(py);
                        input.set_item("port", port.bind(py))?;
                        input.set_item("slot", id)?;
                        inputs.append(input)?;
                    }
                }
                for slot in inf
                    .outs
                    .iter()
                    .flatten()
                    .chain(inf.versions.iter().flatten().map(|v| &v.version))
                {
                    if !budget.take() {
                        break;
                    }
                    let id = slot.borrow().id;
                    slots.entry(id).or_insert(slot.clone());
                    producers.entry(id).or_default().push(inf.id);
                    outputs.append(id)?;
                }
                if inf.state == InfState::Ready
                    && (inf.ins.ordered || (!inf.ins.mutates.is_empty() && mutation_wait))
                {
                    for prior in &infs {
                        if !budget.take() {
                            break;
                        }
                        let prior = prior.borrow();
                        if prior.key < inf.key
                            && (prior.state.live()
                                || (prior.state == InfState::Done && prior.fault.is_some()))
                        {
                            let block = PyDict::new(py);
                            block.set_item("kind", "inf")?;
                            block.set_item("id", prior.id)?;
                            block.set_item("key", prior.key.to_py(py))?;
                            blockers.append(block)?;
                        }
                    }
                    for st in &live {
                        if !budget.take() {
                            break;
                        }
                        let st = st.borrow();
                        if st.state != StrandState::Ended && st.next_key() < inf.key {
                            let block = PyDict::new(py);
                            block.set_item("kind", "strand")?;
                            block.set_item("id", st.id)?;
                            block.set_item("key", st.next_key().to_py(py))?;
                            blockers.append(block)?;
                        }
                    }
                }
                if let Some(collector) = inf
                    .collector
                    .as_ref()
                    .filter(|_| inf.collect_index.is_none())
                {
                    if budget.take() {
                        let slot = collector.borrow().output.clone();
                        let id = slot.borrow().id;
                        slots.entry(id).or_insert(slot);
                        producers.entry(id).or_default().push(inf.id);
                        outputs.append(id)?;
                    }
                }
                data.set_item("inputs", inputs)?;
                data.set_item("outputs", outputs)?;
                data.set_item("effect_blockers", blockers)?;
                inflights.append(data)?;
            }
            for st in &live {
                if !budget.take() {
                    break;
                }
                let st = st.borrow();
                let data = PyDict::new(py);
                data.set_item("id", st.id)?;
                data.set_item("pc", st.pc)?;
                data.set_item("key", st.next_key().to_py(py))?;
                data.set_item("state", st.state.name())?;
                data.set_item("spawner", st.spawn.is_some())?;
                let control = if matches!(st.state, StrandState::Parked | StrandState::Stalled) {
                    self.code
                        .borrow()
                        .instr(st.pc)
                        .and_then(|ins| match &ins.operand {
                            Some(Operand::Name(name)) => {
                                st.names.as_ref().and_then(|m| m.get(*name))
                            }
                            _ => None,
                        })
                } else {
                    None
                };
                let control = control.and_then(|s| slot_at(&s, &st.next_key(), &mut budget));
                let control_id = control.as_ref().map(|s| s.borrow().id);
                if let Some(slot) = control {
                    let id = slot.borrow().id;
                    slots.entry(id).or_insert(slot);
                }
                data.set_item("control_slot", control_id)?;
                let capped = window_limit.is_some_and(|n| j.unfinished as f64 >= n)
                    || retire_limit
                        .is_some_and(|n| (j.window.len() as i64 - j.unfinished) as f64 >= n);
                let mut complete = infs.len() == j.window.len() && live.len() == j.strands.len();
                let oldest = oldest_live.as_ref() == Some(&st.next_key());
                let mut older_open = false;
                for inf in &infs {
                    if !budget.take() {
                        complete = false;
                        break;
                    }
                    let inf = inf.borrow();
                    if inf.key < st.next_key()
                        && (inf.state.live()
                            || (inf.state == InfState::Done && inf.fault.is_some()))
                    {
                        older_open = true;
                        break;
                    }
                }
                data.set_item(
                    "window_blocked",
                    st.state == StrandState::Runnable
                        && complete
                        && capped
                        && (!oldest || (st.spawn.is_some() && older_open)),
                )?;
                for frame in st.frames.iter() {
                    if !budget.take() {
                        break;
                    }
                    if let Some(join) = &frame.join {
                        let join = join.borrow();
                        for slot in join.phis.values().filter(|_| !join.resolved) {
                            if !budget.take() {
                                break;
                            }
                            let id = slot.borrow().id;
                            slots.entry(id).or_insert(slot.clone());
                            owners.entry(id).or_default().push(st.id);
                        }
                    }
                    if let Some(collector) = &frame.collector {
                        if !budget.take() {
                            break;
                        }
                        if !collector.borrow().active {
                            continue;
                        }
                        let slot = collector.borrow().output.clone();
                        let id = slot.borrow().id;
                        slots.entry(id).or_insert(slot);
                        owners.entry(id).or_default().push(st.id);
                    }
                }
                if let Some(spawn) = &st.spawn {
                    let spawn = spawn.borrow();
                    for join in std::iter::once(&spawn.last).chain(spawn.prev.as_ref()) {
                        if !budget.take() {
                            break;
                        }
                        let join = join.borrow();
                        if join.resolved {
                            continue;
                        }
                        for slot in join.phis.values() {
                            if !budget.take() {
                                break;
                            }
                            let id = slot.borrow().id;
                            slots.entry(id).or_insert(slot.clone());
                            owners.entry(id).or_default().push(st.id);
                        }
                    }
                    if let Some(collector) = &spawn.collector {
                        if budget.take() && collector.borrow().active {
                            let slot = collector.borrow().output.clone();
                            let id = slot.borrow().id;
                            slots.entry(id).or_insert(slot);
                            owners.entry(id).or_default().push(st.id);
                        }
                    }
                }
                strands.append(data)?;
            }
            let slots_out = PyList::empty(py);
            for (id, slot) in slots {
                if !budget.take() {
                    break;
                }
                let slot = slot.borrow();
                let data = PyDict::new(py);
                data.set_item("id", id)?;
                data.set_item(
                    "state",
                    match slot.state {
                        SlotState::Pending => "pending",
                        SlotState::Ready => "ready",
                        SlotState::Squashed => "squashed",
                    },
                )?;
                data.set_item("phi", slot.phi)?;
                data.set_item("producers", producers.remove(&id).unwrap_or_default())?;
                let mut own = owners.remove(&id).unwrap_or_default();
                own.sort_unstable();
                own.dedup();
                data.set_item("owners", own)?;
                slots_out.append(data)?;
            }
            if let Some(cut) = &j.cut {
                let checkpoint = PyDict::new(py);
                checkpoint.set_item("phase", "draining")?;
                checkpoint.set_item("request_id", &cut.id)?;
                let held = PyList::empty(py);
                for inf in &cut.held {
                    if !budget.take() {
                        break;
                    }
                    held.append(inf.borrow().id)?;
                }
                checkpoint.set_item("held", held)?;
                checkpoint.set_item("frontier", cut.frontier.as_ref().map(|k| k.to_py(py)))?;
                row.set_item("checkpoint", checkpoint)?;
            } else {
                row.set_item("checkpoint", py.None())?;
            }
            row.set_item("inflights", inflights)?;
            row.set_item("strands", strands)?;
            row.set_item("slots", slots_out)?;
            jobs_out.append(row)?;
        }
        if budget.truncated {
            unsupported.append("bounded evidence omitted")?;
        }
        out.set_item("jobs", jobs_out)?;
        out.set_item("truncated", budget.truncated)?;
        out.set_item("unsupported", unsupported)?;
        Ok(out.unbind())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime::state::Slot;
    #[test]
    fn effective_evidence_is_bounded_and_does_not_compress_forwarding() {
        let start = Slot::phi();
        let middle = Slot::phi();
        let end = Slot::pending();
        start.borrow_mut().forward = Some(middle.clone());
        middle.borrow_mut().forward = Some(end.clone());
        let mut budget = Budget { left: 8, truncated: false };
        assert!(std::rc::Rc::ptr_eq(&slot_at(&start, &Key::last(), &mut budget).unwrap(), &end));
        assert!(std::rc::Rc::ptr_eq(start.borrow().forward.as_ref().unwrap(), &middle));
        let mut budget = Budget { left: 1, truncated: false };
        assert!(slot_at(&start, &Key::last(), &mut budget).is_none());
        assert!(budget.truncated);
    }
    #[test]
    fn evidence_uses_only_older_unsquashed_mutation_versions() {
        let base = Slot::pending();
        let old = Slot::pending();
        let young = Slot::pending();
        base.borrow_mut().muts = vec![(Key::of(&[], 2), old.clone()), (Key::of(&[], 8), young)];
        let mut budget = Budget { left: 12, truncated: false };
        assert!(std::rc::Rc::ptr_eq(
            &slot_at(&base, &Key::of(&[], 5), &mut budget).unwrap(),
            &old
        ));
        assert_eq!(base.borrow().muts.len(), 2);
    }
}
