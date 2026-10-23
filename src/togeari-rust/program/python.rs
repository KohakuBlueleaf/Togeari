//! Import of one region of the Python code memory (`togeari.core.program.code`): its
//! instructions, decode tables and `@try` tables, read once when a job first starts in it.

use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString, PyTuple};

use super::core::{
    Binding, Code, Instr, NameSet, Obj, Op, Operand, Region, StoreMode, Table, TryInfo,
};

const ALL: &str = "*";

fn opt_i64(v: &Bound<'_, PyAny>) -> PyResult<Option<i64>> {
    if v.is_none() {
        Ok(None)
    } else {
        Ok(Some(v.extract()?))
    }
}

fn opt_name(code: &mut Code, v: &Bound<'_, PyAny>) -> PyResult<Option<u32>> {
    if v.is_none() {
        Ok(None)
    } else {
        let s: String = v.extract()?;
        Ok(Some(code.intern(v.py(), &s)))
    }
}

/// A frozenset of names (with the `ALL` marker) as a name set.
fn name_set(code: &mut Code, v: &Bound<'_, PyAny>) -> PyResult<NameSet> {
    let py = v.py();
    let mut all = false;
    let mut ids = Vec::new();
    for x in v.try_iter()? {
        let s: String = x?.extract()?;
        if s == ALL {
            all = true;
        } else {
            ids.push(code.intern(py, &s));
        }
    }
    Ok(NameSet::new(all, ids))
}

fn table(code: &mut Code, v: &Bound<'_, PyAny>) -> PyResult<Rc<Table>> {
    let d = v.cast::<PyDict>()?;
    let mut t = Table::with_capacity(d.len());
    for (k, w) in d.iter() {
        t.insert(k.extract()?, name_set(code, &w)?);
    }
    Ok(Rc::new(t))
}

fn is_container(v: &Bound<'_, PyAny>) -> bool {
    v.is_instance_of::<PyList>() || v.is_instance_of::<PyDict>() || v.is_instance_of::<PySet>()
}

fn operand(code: &mut Code, v: &Bound<'_, PyAny>) -> PyResult<Option<Operand>> {
    if v.is_none() {
        return Ok(None);
    }
    let t = v.cast::<PyTuple>()?;
    let kind: String = t.get_item(0)?.extract()?;
    let x = t.get_item(1)?;
    Ok(Some(match kind.as_str() {
        "n" => {
            let s: String = x.extract()?;
            Operand::Name(code.intern(v.py(), &s))
        }
        "l" | "d" => Operand::Lit { container: is_container(&x), value: Rc::new(x.unbind()) },
        _ => Operand::Missing(x.str()?.unbind()),
    }))
}

fn binding(b: &Bound<'_, PyAny>) -> PyResult<Option<Binding>> {
    if b.is_none() {
        return Ok(None);
    }
    let py = b.py();
    let inline: bool = b.getattr(intern!(py, "inline"))?.extract()?;
    let opt = |name: &str| -> PyResult<Option<Obj>> {
        let v = b.getattr(name)?;
        Ok(if v.is_none() { None } else { Some(v.unbind()) })
    };
    let lane = opt("lane")?;
    let lane_name = match &lane {
        Some(l) => Some(l.bind(py).getattr(intern!(py, "name"))?.str()?.unbind()),
        None => None,
    };
    let store: Option<String> = b.getattr(intern!(py, "store"))?.extract()?;
    let store = match store.as_deref() {
        Some("content") => StoreMode::Content,
        Some("positional") => StoreMode::Positional,
        _ => StoreMode::No,
    };
    Ok(Some(Binding {
        inline,
        func: opt("fn")?,
        lane,
        lane_name,
        adapter: opt("adapter")?,
        store,
        idempotent: b.getattr(intern!(py, "idempotent"))?.is_truthy()?,
        warn_us: b.getattr(intern!(py, "warn_us"))?.extract()?,
        timeout_s: b
            .getattr(intern!(py, "policy"))?
            .getattr(intern!(py, "timeout_s"))?
            .extract()?,
        policy_source: b.getattr(intern!(py, "policy_source"))?.extract()?,
    }))
}

fn instr(code: &mut Code, ins: &Bound<'_, PyAny>) -> PyResult<Instr> {
    let py = ins.py();
    let g = |name: &str| ins.getattr(name);
    let op_s: String = g("op")?.extract()?;
    let op = Op::parse(&op_s)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err(format!("unknown op {op_s}")))?;

    // Call fields
    let mut args = Vec::new();
    let mut ports: Vec<String> = Vec::new();
    for a in g("args")?.try_iter()? {
        let a = a?;
        let t = a.cast::<PyTuple>()?;
        let port = t.get_item(0)?.str()?;
        ports.push(port.to_string());
        let o = operand(code, &t.get_item(1)?)?.expect("an argument operand");
        args.push((port.unbind(), o));
    }
    let mut outs = Vec::new();
    for o in g("outs")?.try_iter()? {
        outs.push(opt_name(code, &o?)?);
    }
    let sig = g("signature")?;
    let mut sig_outputs: Vec<String> = Vec::new();
    if !sig.is_none() {
        for p in sig.getattr(intern!(py, "outputs"))?.try_iter()? {
            sig_outputs.push(p?.getattr(intern!(py, "name"))?.extract()?);
        }
    }
    let mut mutates = Vec::new();
    let mut mutates_args = Vec::new();
    let mut mutates_out = Vec::new();
    for m in g("mutates")?.try_iter()? {
        let m = m?;
        let port = m.cast::<PyTuple>()?.get_item(0)?.str()?;
        let p = port.to_string();
        mutates_args.push(ports.iter().rposition(|x| *x == p));
        mutates_out.push(sig_outputs.iter().position(|x| *x == p));
        mutates.push(port.unbind());
    }
    let hint = g("hint")?.cast_into::<PyDict>()?;
    let hint_priority = hint.get_item("priority")?.map(|v| v.unbind());
    let hint_rank = match &hint_priority {
        None => Some(0),
        Some(v) if v.bind(py).is_instance_of::<pyo3::types::PyInt>() => v.extract(py).ok(),
        Some(_) => None,
    };

    // Control fields
    let mut cases = Vec::new();
    for c in g("cases")?.try_iter()? {
        let c = c?;
        let t = c.cast::<PyTuple>()?;
        cases.push((t.get_item(0)?.unbind(), t.get_item(1)?.extract()?));
    }
    let mut targets = Vec::new();
    for t in g("targets")?.try_iter()? {
        targets.push(t?.extract()?);
    }
    let raw_operand = g("operand")?;
    let operand_name = if raw_operand.is_none() {
        None
    } else {
        Some(raw_operand.cast::<PyTuple>()?.get_item(1)?.repr()?.unbind())
    };
    let try_id = g("try_id")?;
    let try_id = if try_id.is_none() {
        None
    } else {
        Some(try_id.extract::<(i64, i64)>()?)
    };

    Ok(Instr {
        addr: g("addr")?.extract()?,
        op,
        py: ins.clone().unbind(),
        next: opt_i64(&g("next")?)?,
        targets,
        cases,
        default: opt_i64(&g("default")?)?,
        hint: hint.unbind(),
        hint_priority,
        hint_rank,
        node: g("node")?.str()?.unbind(),
        args,
        outs,
        n_sig_out: g("n_sig_out")?.extract()?,
        sig_outputs,
        ordered: g("ordered")?.is_truthy()?,
        mutates,
        mutates_args,
        mutates_out,
        binding: binding(&g("binding")?)?,
        target: {
            let t: String = g("target")?.extract()?;
            if t.is_empty() {
                None
            } else {
                Some(code.intern(py, &t))
            }
        },
        operand: operand(code, &raw_operand)?,
        operand_name,
        item: opt_name(code, &g("item")?)?,
        index: opt_name(code, &g("index")?)?,
        body: opt_i64(&g("body")?)?,
        after: opt_i64(&g("after")?)?,
        iter_end: opt_i64(&g("iter_end")?)?,
        has_exit: g("has_exit")?.is_truthy()?,
        each_addr: opt_i64(&g("each_addr")?)?,
        collect_value: operand(code, &g("collect_value")?)?,
        collect_target: opt_name(code, &g("collect_target")?)?,
        try_id,
        reconv: None,
        structured: false,
        may_write: NameSet::default(),
        region_writes: None,
        body_may_write: NameSet::default(),
        body_writes: None,
        carried: NameSet::default(),
    })
}

/// Import ``region`` (a committed `Region` of ``mem``) unless it is already imported.
pub fn import_region(
    code: &mut Code,
    mem: &Bound<'_, PyAny>,
    region: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = region.py();
    let start: i64 = region.getattr(intern!(py, "start"))?.extract()?;
    if code.has_region(start) {
        return Ok(());
    }
    let end: i64 = region.getattr(intern!(py, "end"))?.extract()?;
    let tables = region.getattr(intern!(py, "tables"))?;
    let reconv = tables
        .getattr(intern!(py, "reconv"))?
        .cast_into::<PyDict>()?;

    // Instructions, with the decode tables of control instructions
    if code.instrs.len() < end as usize {
        code.instrs.resize_with(end as usize, || None);
    }
    for a in start..end {
        let ins_py = mem.get_item(a)?;
        let mut ins = instr(code, &ins_py)?;
        if matches!(ins.op, Op::Branch | Op::Switch | Op::Each) {
            ins.reconv = match reconv.get_item(a)? {
                Some(v) => opt_i64(&v)?,
                None => None,
            };
            ins.structured = tables
                .call_method1(intern!(py, "structured"), (a,))?
                .is_truthy()?;
            ins.region_writes =
                Some(table(code, &tables.call_method1(intern!(py, "region_writes"), (a,))?)?);
            ins.may_write = name_set(code, &tables.call_method1(intern!(py, "may_write"), (a,))?)?;
            if ins.op == Op::Each {
                let bw = tables.call_method1(intern!(py, "body_writes"), (a,))?;
                ins.body_writes = Some(table(code, &bw)?);
                let bmw = tables.call_method1(intern!(py, "body_may_write"), (a,))?;
                ins.body_may_write = name_set(code, &bmw)?;
                let c = tables.call_method1(intern!(py, "carried"), (a,))?;
                ins.carried = name_set(code, &c)?;
                ins.has_exit = ins_py.getattr(intern!(py, "has_exit"))?.is_truthy()?;
            }
        }
        code.instrs[a as usize] = Some(Rc::new(ins));
    }

    // `@try` tables of the region
    let tries = region
        .getattr(intern!(py, "tries"))?
        .cast_into::<PyDict>()?;
    let mut out: HashMap<(i64, i64), Rc<TryInfo>> = HashMap::new();
    for (tid, info) in tries.iter() {
        let tid: (i64, i64) = tid.extract()?;
        let mut body = HashSet::new();
        for b in info.getattr(intern!(py, "body"))?.try_iter()? {
            body.insert(b?.extract::<i64>()?);
        }
        let error_name = opt_name(code, &info.getattr(intern!(py, "error_name"))?)?;
        let handler: i64 = info.getattr(intern!(py, "handler"))?.extract()?;
        out.insert(tid, Rc::new(TryInfo { body, handler, error_name }));
    }
    code.tries.extend(out);

    let hash = region
        .getattr(intern!(py, "semantic_hash"))?
        .cast_into::<PyString>()?
        .unbind();
    code.add_region(Region { start, end, hash });
    Ok(())
}
