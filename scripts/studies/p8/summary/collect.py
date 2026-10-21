"""Every variant's numbers, read from the benches' outputs, as ``stat``s over the samples a bench
has for one shape (its rounds, or its repeats over the rounds), and every failure a variant met:
a failed child, a failed job, a missing output."""

import json
import statistics
from pathlib import Path

from p8.contention import report as gil_report


def stat(values: list[float]) -> dict | None:
    """Median, min, max and count of ``values`` (None when there is none)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {"median": statistics.median(vals), "min": min(vals), "max": max(vals), "n": len(vals)}


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def ceiling(path: Path) -> dict:
    """{shape: {setting: {variant: {calls (stat over rounds), us_per_call, busy, ok, offers_per_s
    (stat)}}}}."""
    doc = _read(path) or {}
    out: dict = {}
    for variant, summary in doc.get("by_variant", {}).items():
        for shape, by_setting in summary.items():
            for setting, s in by_setting.items():
                rows = [
                    r
                    for r in doc["rows"]
                    if r["variant"] == variant and r["shape"] == shape and r["setting"] == setting
                ]
                calls = stat([r["calls_per_s"] for r in rows])
                offers = [
                    r["native_stats"]["offers"] / r["seconds"]
                    for r in rows
                    if r.get("native_stats")
                ]
                out.setdefault(shape, {}).setdefault(setting, {})[variant] = {
                    "calls": calls,
                    "us_per_call": 1e6 / calls["median"] if calls else None,
                    "busy": s["dispatcher_busy"],
                    "ok": s["ok"],
                    "offers_per_s": stat(offers),
                }
    return out


def _round_files(root: Path, pattern: str) -> list[Path]:
    """Every round's output under ``root`` matching ``pattern``, in round order."""
    return sorted(root.glob(pattern))


def scaling(root: Path, variants: list[str]) -> tuple[dict, dict]:
    """{"<shape> <n>": {variant: us per job or item (stat over every measured repeat of every
    round)}}, and per shape and variant the Rust core's counts per job or item."""
    us: dict = {}
    native: dict = {}
    for v in variants:
        samples: dict[str, list] = {}
        runs: dict[str, list] = {}
        for path in _round_files(root / v, "scaling-r*.json"):
            for r in (_read(path) or {}).get("rows", []):
                key = f"{r['shape']} {r['n']}"
                samples.setdefault(key, []).extend(r.get("us_runs") or [r["us"]])
                runs.setdefault(key, []).extend((r["n"], x) for x in r.get("runs") or [])
        for key, xs in samples.items():
            us.setdefault(key, {})[v] = stat(xs)
            per = {}
            for name in ("offers", "regions_released", "regions_held"):
                per[f"{name}_per_unit"] = stat(
                    [x["native_stats"][name] / n for n, x in runs[key] if x.get("native_stats")]
                )
            per["offers_per_s"] = stat(
                [
                    x["native_stats"]["offers"] / x["seconds"]
                    for _, x in runs[key]
                    if x.get("native_stats")
                ]
            )
            per["cycles_per_unit"] = stat([x["cycles"] / n for n, x in runs[key]])
            native.setdefault(key, {})[v] = per
    return us, native


def p6(root: Path, variants: list[str], scale: str, study: str) -> dict:
    """{config: {variant: {calls, dispatcher_us (stats over rounds), jobs_per_s, failed (rounds
    that raised), short (jobs not ok)}}}, every round's results merged."""
    out: dict = {}
    for v in variants:
        runs: dict[str, list] = {}
        failed: dict[str, int] = {}
        for path in _round_files(root / v, f"p6-r*/{scale}/{study}/results.json"):
            for r in (_read(path) or {}).get("results", []):
                name = r["config"]["name"]
                runs.setdefault(name, [])
                if "error" in r:
                    failed[name] = failed.get(name, 0) + 1
                else:
                    runs[name].append(r["result"])
        for name, xs in runs.items():
            out.setdefault(name, {})[v] = {
                "calls": stat([x["calls_per_s"] for x in xs]),
                "dispatcher_us": stat([x.get("dispatcher_us_per_call") for x in xs]),
                "jobs_per_s": stat([x["jobs_per_s"] for x in xs]),
                "failed": failed.get(name, 0),
                "short": sum(x["jobs"] - x["jobs_ok"] for x in xs),
            }
    return out


def gil(results: list[dict], loads: list[str]) -> dict:
    """{"with_work": {"work / isolation": {load: {variant: {rate (stat over rounds), share,
    calls_per_s, offers_per_s}}}}, "alone": {...: {load: {variant: {calls_per_s, busy,
    offers_per_s}}}}, "errors": [...], "failures": {variant: [...]}}."""
    groups: dict[tuple, list] = {}
    errors = []
    failures: dict[str, list] = {}
    for r in results:
        cfg = r["config"]
        v = cfg.get("variant") or cfg.get("core")
        if r.get("skipped") or r.get("error"):
            text = gil_report.label(r) + f": {r.get('skipped') or r.get('error')}"
            errors.append(text)
            failures.setdefault(v, []).append(f"GIL {text}")
            continue
        for p in r.get("phases", []):
            d = p.get("dispatcher") or {}
            if p.get("error"):
                failures.setdefault(v, []).append(f"GIL {gil_report.label(r)} {p['phase']}: error")
            elif d.get("jobs_failed"):
                failures.setdefault(v, []).append(
                    f"GIL {gil_report.label(r)} {p['phase']}: {d['jobs_failed']} jobs failed"
                )
        key = f"{cfg['work']} / {cfg['isolation']}"
        groups.setdefault((key, v), []).append(r)

    def offers(d: dict | None) -> float | None:
        return ((d or {}).get("native") or {}).get("offers_per_s")

    out: dict = {"with_work": {}, "alone": {}, "errors": errors, "failures": failures}
    for (key, v), rs in groups.items():
        idle = stat([gil_report.work_rate(r, "idle") for r in rs])
        for load in loads:
            rate = stat([gil_report.work_rate(r, load) for r in rs])
            ds = [gil_report.dispatcher(r, f"work+{load}") for r in rs]
            out["with_work"].setdefault(key, {}).setdefault(load, {})[v] = {
                "rate": rate,
                "share": rate["median"] / idle["median"] if rate and idle else None,
                "calls_per_s": stat([d["calls_per_s"] for d in ds if d]),
                "offers_per_s": stat([offers(d) for d in ds if d]),
            }
        for load in loads[1:]:
            ds = [d for r in rs if (d := gil_report.dispatcher(r, f"{load} alone"))]
            out["alone"].setdefault(key, {}).setdefault(load, {})[v] = {
                "calls_per_s": stat([d["calls_per_s"] for d in ds]),
                "busy": stat([d["busy_fraction"] for d in ds]),
                "offers_per_s": stat([offers(d) for d in ds]),
            }
    return out


def failures(data: dict, steps: list[dict], variants: list[str]) -> dict[str, list[str]]:
    """Per variant, every failure the run met: a child that did not exit 0 (or timed out), a
    ceiling shape with a job not ok, a P6 round that raised or a job not ok, a GIL config or
    phase that failed. A step of every variant (parity, the ceiling) fails each of them."""
    out: dict[str, list[str]] = {v: [] for v in variants}
    for s in steps:
        if s.get("exit_code") in (0, None):
            continue
        who = [s["variant"]] if s.get("variant") in out else variants
        for v in who:
            out[v].append(f"step {s['name']}: exit {s['exit_code']}")
    for shape, by_setting in data["ceiling"].items():
        for setting, by_v in by_setting.items():
            for v, x in by_v.items():
                if v in out and not x["ok"]:
                    out[v].append(f"ceiling {shape} ({setting}): a job not ok")
    for study, by_name in data["p6"].items():
        for name, by_v in by_name.items():
            for v, x in by_v.items():
                if v in out and (x["failed"] or x["short"]):
                    out[v].append(
                        f"P6 {study} {name}: {x['failed']} rounds raised, {x['short']} jobs not ok"
                    )
    for v, xs in data["gil"]["failures"].items():
        if v in out:
            out[v] += xs
    return out
