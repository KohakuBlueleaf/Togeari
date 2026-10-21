"""The GIL study's tables from its config results: engine items/s at each dispatcher load."""

from pathlib import Path


def _phase(cfg: dict, name: str) -> dict | None:
    return next((p for p in cfg.get("phases", []) if p.get("phase") == name), None)


def work_rate(cfg: dict, load: str) -> float | None:
    p = _phase(cfg, f"work+{load}")
    if p is None or p.get("error"):
        return None
    return p["feeders"]["work"]["items_ok_per_s"]


def dispatcher(cfg: dict, phase: str) -> dict | None:
    p = _phase(cfg, phase)
    if p is None or p.get("error") or p.get("dispatcher") is None:
        return None
    return p["dispatcher"]


def label(r: dict) -> str:
    """``variant / work / isolation`` (the core that ran when the config names no variant)."""
    cfg = r["config"]
    who = cfg.get("variant") or r.get("core") or cfg.get("core") or "python"
    rnd = f" r{cfg['round']}" if cfg.get("round") is not None else ""
    return f"{who} / {cfg['work']} / {cfg['isolation']}{rnd}"


def rows(results: list[dict], loads: list[str]) -> list[list[str]]:
    """Per config: items/s at each load (with the share of idle), then the dispatcher alone."""
    out = []
    for r in results:
        name = label(r)
        if r.get("skipped") or r.get("error"):
            out.append([name, r.get("skipped") or "failed", *[""] * (len(loads) - 1)])
            continue
        idle = work_rate(r, "idle")
        cells = []
        for load in loads:
            rate = work_rate(r, load)
            d = dispatcher(r, f"work+{load}")
            if rate is None:
                cells.append("failed")
                continue
            share = f" ({rate / idle:.0%})" if idle else ""
            calls = f", {d['calls_per_s']:,.0f} calls/s busy {d['busy_fraction']:.2f}" if d else ""
            cells.append(f"{rate:,.0f}{share}{calls}")
        out.append([name, *cells])
    return out


def alone(results: list[dict], loads: list[str]) -> list[list[str]]:
    """Per config: the dispatcher's calls/s and busy fraction at each load with no engine work."""
    out = []
    for r in results:
        if r.get("skipped") or r.get("error"):
            continue
        name = label(r)
        cells = []
        for load in loads[1:]:
            d = dispatcher(r, f"{load} alone")
            cells.append(f"{d['calls_per_s']:,.0f} (busy {d['busy_fraction']:.2f})" if d else "-")
        out.append([name, *cells])
    return out


def table(header: list[str], body: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


def write(root: Path, title: str, results: list[dict], loads: list[str]) -> Path:
    text = [
        f"# {title}",
        "",
        "Engine items/s fed directly (share of its idle rate), with the dispatcher's calls/s",
        "and busy fraction in the same window:",
        "",
        table(["variant / work / isolation", *loads], rows(results, loads)),
        "",
        "The dispatcher alone at each load (calls/s, busy fraction):",
        "",
        table(["variant / work / isolation", *loads[1:]], alone(results, loads)),
        "",
    ]
    path = root / "summary.md"
    path.write_text("\n".join(text), encoding="utf-8")
    return path
