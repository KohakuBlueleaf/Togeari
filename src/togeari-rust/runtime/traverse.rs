//! The collector's view of the core: every Python object the core holds, each reference
//! visited once (shared Rust objects are walked once), and the teardown that drops them.

use std::collections::HashSet;
use std::rc::Rc;

use pyo3::{PyTraverseError, PyVisit};

use crate::program::core::{Code, Instr, Operand};
use crate::runtime::core::{Core, Pend};
use crate::runtime::state::frame::Frames;
use crate::runtime::state::names::Dict;
use crate::runtime::state::{
    EachSpawn, Entry, InfRef, JobRef, JoinRef, Names, SlotRef, StrandRef, Val, Waiter,
};

type Res = Result<(), PyTraverseError>;

enum Node {
    Job(JobRef),
    Strand(StrandRef),
    Inf(InfRef),
    Slot(SlotRef),
    Join(JoinRef),
    Frames(Frames),
    Dict(Dict),
    Spawn(Rc<std::cell::RefCell<EachSpawn>>),
    Entry(Rc<Entry>),
    Instr(Rc<Instr>),
}

/// A walk over the core's objects; RefCells already borrowed are skipped (visiting less is
/// safe, visiting a reference twice is not).
struct Tr<'a> {
    visit: PyVisit<'a>,
    seen: HashSet<usize>,
    stack: Vec<Node>,
}

impl<'a> Tr<'a> {
    fn first<T>(&mut self, rc: &Rc<T>) -> bool {
        self.seen.insert(Rc::as_ptr(rc) as *const u8 as usize)
    }

    fn val(&mut self, v: &Val) -> Res {
        if self.first(v) {
            self.visit.call(&**v)?;
        }
        Ok(())
    }

    fn names(&mut self, n: &Names) {
        self.stack.push(Node::Dict(n.base.clone()));
        self.stack.push(Node::Dict(n.delta.clone()));
    }

    fn push(&mut self, n: Node) {
        self.stack.push(n);
    }

    fn run(&mut self) -> Res {
        while let Some(n) = self.stack.pop() {
            match n {
                Node::Job(j) if self.first(&j) => self.job(&j)?,
                Node::Strand(s) if self.first(&s) => self.strand(&s),
                Node::Inf(x) if self.first(&x) => self.inf(&x)?,
                Node::Slot(s) if self.first(&s) => self.slot(&s)?,
                Node::Join(j) if self.first(&j) => {
                    if let Ok(j) = j.try_borrow() {
                        for s in j.phis.values() {
                            self.stack.push(Node::Slot(s.clone()));
                        }
                    }
                }
                Node::Frames(f) if self.first(&f) => {
                    for fr in f.iter() {
                        if !self.first(fr) {
                            continue;
                        }
                        if let Some(j) = &fr.join {
                            self.stack.push(Node::Join(j.clone()));
                        }
                        if let Some(items) = &fr.items {
                            if self.first(items) {
                                self.visit.call(&**items)?;
                            }
                        }
                    }
                }
                Node::Dict(d) if self.first(&d) => {
                    for s in d.values() {
                        self.stack.push(Node::Slot(s.clone()));
                    }
                }
                Node::Spawn(e) if self.first(&e) => self.spawn(&e)?,
                Node::Entry(e) if self.first(&e) => entry(&self.visit, &e)?,
                Node::Instr(i) if self.first(&i) => self.instr(&i)?,
                _ => {}
            }
        }
        Ok(())
    }

    fn job(&mut self, j: &JobRef) -> Res {
        let Ok(j) = j.try_borrow() else { return Ok(()) };
        let v = &self.visit;
        v.call(&j.key)?;
        v.call(&j.inputs)?;
        if let Some(f) = &j.failure {
            v.call(f)?;
        }
        if let Some(p) = &j.stats.program {
            v.call(p)?;
        }
        for x in j.stats.extra.values() {
            v.call(x)?;
        }
        if let Some(r) = &j.record {
            v.call(r)?;
        }
        if let Some(h) = &j.handle {
            v.call(h)?;
        }
        if let Some(d) = &j.inputs_dig {
            v.call(d)?;
        }
        if let Some(m) = &j.memo {
            for es in m.entries.values() {
                for (d, x, _) in es {
                    v.call(d)?;
                    v.call(x)?;
                }
            }
        }
        self.stack.push(Node::Entry(j.entry.clone()));
        for s in j.strands.values() {
            self.stack.push(Node::Strand(s.clone()));
        }
        for e in j.sheap.iter() {
            self.stack.push(Node::Strand(e.0 .2 .0.clone()));
        }
        for s in &j.runnable {
            self.stack.push(Node::Strand(s.clone()));
        }
        for x in j.window.items.iter().chain(j.held.iter()) {
            self.stack.push(Node::Inf(x.clone()));
        }
        for e in j.wheap.iter() {
            self.stack.push(Node::Inf(e.0 .2 .0.clone()));
        }
        if let Some(n) = &j.final_names {
            self.names(n);
        }
        Ok(())
    }

    fn strand(&mut self, s: &StrandRef) {
        let Ok(s) = s.try_borrow() else { return };
        self.stack.push(Node::Frames(s.frames.clone()));
        if let Some(n) = &s.names {
            self.stack.push(Node::Dict(n.base.clone()));
            self.stack.push(Node::Dict(n.delta.clone()));
        }
        if let Some(e) = &s.spawn {
            self.stack.push(Node::Spawn(e.clone()));
        }
    }

    fn spawn(&mut self, e: &Rc<std::cell::RefCell<EachSpawn>>) -> Res {
        let Ok(e) = e.try_borrow() else { return Ok(()) };
        if self.first(&e.items) {
            self.visit.call(&*e.items)?;
        }
        for v in e.vals.iter().chain(e.idx.iter()) {
            self.val(v)?;
        }
        self.names(&e.base);
        self.stack.push(Node::Frames(e.frames.clone()));
        self.stack.push(Node::Join(e.last.clone()));
        if let Some(p) = &e.prev {
            self.stack.push(Node::Join(p.clone()));
        }
        Ok(())
    }

    fn slot(&mut self, s: &SlotRef) -> Res {
        let Ok(s) = s.try_borrow() else { return Ok(()) };
        if let Some(v) = &s.value {
            self.val(v)?;
        }
        for w in &s.waiters {
            match w {
                Waiter::Strand(st) => self.stack.push(Node::Strand(st.clone())),
                Waiter::Inf(x) => self.stack.push(Node::Inf(x.clone())),
            }
        }
        if let Some(f) = &s.forward {
            self.stack.push(Node::Slot(f.clone()));
        }
        for (_, m) in &s.muts {
            self.stack.push(Node::Slot(m.clone()));
        }
        Ok(())
    }

    fn inf(&mut self, x: &InfRef) -> Res {
        let Ok(x) = x.try_borrow() else { return Ok(()) };
        let v = &self.visit;
        if let Some(f) = &x.fault {
            v.call(&f.error)?;
        }
        if let Some(o) = &x.item {
            v.call(o)?;
        }
        if let Some(o) = &x.owner {
            v.call(o)?;
        }
        if let Some(o) = &x.values {
            v.call(o)?;
        }
        for o in [&x.result, &x.kept, &x.mutated, &x.store_key, &x.dig]
            .into_iter()
            .flatten()
        {
            v.call(o)?;
        }
        self.stack.push(Node::Instr(x.ins.clone()));
        for s in x.inputs.iter().chain(x.outs.iter().flatten()) {
            self.stack.push(Node::Slot(s.clone()));
        }
        for ver in x.versions.iter().flatten() {
            self.stack.push(Node::Slot(ver.before.clone()));
            self.stack.push(Node::Slot(ver.version.clone()));
        }
        if let Some(s) = &x.snapshot {
            self.names(&s.names);
            self.stack.push(Node::Frames(s.frames.clone()));
        }
        Ok(())
    }
}

fn entry(v: &PyVisit<'_>, e: &Entry) -> Res {
    v.call(&e.py_name)?;
    v.call(&e.region_hash)?;
    v.call(&e.py)?;
    for p in &e.params {
        v.call(&p.py_name)?;
        v.call(&p.default)?;
    }
    for (o, _) in e.outputs.iter().flatten() {
        v.call(o)?;
    }
    Ok(())
}

impl Tr<'_> {
    /// A literal's value is shared (by Rc) with the slots made from it.
    fn operand(&mut self, o: &Operand) -> Res {
        match o {
            Operand::Lit { value, .. } => self.val(value),
            Operand::Missing(p) => self.visit.call(p),
            Operand::Name(_) => Ok(()),
        }
    }

    fn instr(&mut self, i: &Instr) -> Res {
        let v = &self.visit;
        v.call(&i.py)?;
        v.call(&i.hint)?;
        v.call(&i.node)?;
        for o in [&i.hint_timeout, &i.hint_priority].into_iter().flatten() {
            v.call(o)?;
        }
        for (c, _) in &i.cases {
            v.call(c)?;
        }
        for m in &i.mutates {
            v.call(m)?;
        }
        if let Some(n) = &i.operand_name {
            v.call(n)?;
        }
        if let Some(b) = &i.binding {
            for o in [&b.func, &b.lane, &b.adapter].into_iter().flatten() {
                v.call(o)?;
            }
            if let Some(n) = &b.lane_name {
                v.call(n)?;
            }
        }
        for (p, o) in &i.args {
            self.visit.call(p)?;
            self.operand(o)?;
        }
        if let Some(o) = &i.operand {
            self.operand(o)?;
        }
        Ok(())
    }
}

fn code(tr: &mut Tr<'_>, c: &Code) -> Res {
    for n in &c.py_names {
        tr.visit.call(n)?;
    }
    for r in &c.regions {
        tr.visit.call(&r.hash)?;
    }
    for i in c.instrs.iter().flatten() {
        tr.push(Node::Instr(i.clone()));
    }
    Ok(())
}

impl Core {
    /// Visit every object of the core: its host, job state, code and caches.
    pub fn traverse(&self, visit: PyVisit<'_>) -> Res {
        let mut tr = Tr { visit, seen: HashSet::new(), stack: Vec::new() };
        self.h.visit(&tr.visit)?;
        tr.val(&self.h.undef_val)?;
        if let Ok(c) = self.clock.try_borrow() {
            tr.visit.call(&*c)?;
        }
        if let Ok(i) = self.inbox.try_borrow() {
            if let Some(i) = &*i {
                tr.visit.call(i)?;
            }
        }
        if let Ok(c) = self.code.try_borrow() {
            code(&mut tr, &c)?;
        }
        if let Ok(e) = self.entries.try_borrow() {
            for (o, en) in e.values() {
                tr.visit.call(o)?;
                tr.push(Node::Entry(en.clone()));
            }
        }
        if let Ok(l) = self.lanes.try_borrow() {
            for (o, n) in l.values() {
                tr.visit.call(o)?;
                if let Some(n) = n {
                    tr.visit.call(n)?;
                }
            }
        }
        if let Ok(s) = self.shell.try_borrow() {
            if let Some(s) = &*s {
                tr.visit.call(s)?;
            }
        }
        tr.visit.call(&self.keys)?;
        if let Ok(j) = self.jobs.try_borrow() {
            for job in j.values() {
                tr.push(Node::Job(job.clone()));
            }
        }
        if let Ok(j) = self.last_started.try_borrow() {
            if let Some(job) = &*j {
                tr.push(Node::Job(job.clone()));
            }
        }
        if let Ok(r) = self.ready.try_borrow() {
            for x in r.iter() {
                tr.push(Node::Inf(x.clone()));
            }
        }
        if let Ok(b) = self.blocked.try_borrow() {
            for xs in b.values() {
                for x in xs {
                    tr.push(Node::Inf(x.clone()));
                }
            }
        }
        if let Ok(p) = self.pending.try_borrow() {
            for e in p.iter() {
                if let Pend::Stmt { inf, .. } = e {
                    tr.push(Node::Inf(inf.clone()));
                }
            }
        }
        tr.run()
    }

    /// Drop the job state, code and caches (the collector breaks a cycle through the core).
    pub fn teardown(&self) {
        let jobs: Vec<JobRef> = match self.jobs.try_borrow_mut() {
            Ok(mut j) => j.drain(..).map(|(_, j)| j).collect(),
            Err(_) => return,
        };
        for job in jobs {
            Core::let_go(&job);
        }
        if let Ok(mut j) = self.last_started.try_borrow_mut() {
            if let Some(job) = j.take() {
                Core::let_go(&job);
            }
        }
        for cell in [&self.ready] {
            if let Ok(mut r) = cell.try_borrow_mut() {
                r.clear();
            }
        }
        if let Ok(mut b) = self.blocked.try_borrow_mut() {
            b.clear();
        }
        if let Ok(mut p) = self.pending.try_borrow_mut() {
            p.clear();
        }
        if let Ok(mut e) = self.entries.try_borrow_mut() {
            e.clear();
        }
        if let Ok(mut l) = self.lanes.try_borrow_mut() {
            l.clear();
        }
        if let Ok(mut c) = self.code.try_borrow_mut() {
            *c = Code::default();
        }
        if let Ok(mut o) = self.owned.try_borrow_mut() {
            o.clear();
        }
    }

    /// A job's strands and statements let go of everything they hold (its Rc cycles break).
    pub fn let_go(job: &JobRef) {
        let (strands, window) = {
            let Ok(mut j) = job.try_borrow_mut() else {
                return;
            };
            let strands: Vec<StrandRef> = j.strands.values().cloned().collect();
            let mut window = j.window.clear();
            window.append(&mut j.held);
            j.sheap.clear();
            j.wheap.clear();
            j.runnable.clear();
            j.final_names = None;
            j.memo = None;
            (strands, window)
        };
        for st in strands {
            if let Ok(mut j) = job.try_borrow_mut() {
                j.end_strand(&st);
            }
        }
        for x in window {
            if let Ok(mut b) = x.try_borrow_mut() {
                b.release();
            }
        }
    }
}
