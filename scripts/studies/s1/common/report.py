"""``summary.md``: tables generated from ``env.json`` and ``m1.json`` .. ``m5.json``."""

import json
from pathlib import Path
from typing import Any


def fmt(x: Any, digits: int = 3) -> str:
    match x:
        case None:
            return "-"
        case bool():
            return str(x)
        case int():
            return str(x)
        case float() if abs(x) >= 1000:
            return f"{x:,.0f}"
        case float():
            return f"{x:.{digits}g}"
        case _:
            return str(x)


def get(d: Any, *path: str) -> Any:
    """``d[p0][p1]...``, None as soon as a step is missing."""
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(fmt(c) for c in row) + " |" for row in rows]
    return out + [""]


def last_line(text: str | None) -> str:
    """The last non-empty line (a traceback's exception)."""
    if not text:
        return ""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def phase_rows(doc: dict, work: str = "work") -> list[list[Any]]:
    """Per config and phase: the work feeder, the dispatcher, CPU and device numbers."""
    rows = []
    for c in doc["configs"]:
        for ph in c.get("phases", []):
            if ph.get("error"):
                rows.append([c["name"], ph["phase"], "ERROR", *[None] * 11])
                continue

            w = get(ph, "feeders", work)
            d = ph.get("dispatcher")
            rows.append(
                [
                    c["name"],
                    ph["phase"],
                    get(w, "items_ok_per_s"),
                    get(w, "latency_ms", "p50"),
                    get(w, "latency_ms", "p99"),
                    get(ph, "feeders", "chain", "items_ok_per_s"),
                    get(d, "calls_per_s"),
                    get(d, "busy_fraction"),
                    get(d, "thread_cpu_fraction"),
                    get(ph, "cpu", "process_cores"),
                    get(ph, "probes", "gpu", "after", "max_allocated_mb"),
                    get(ph, "gpu", "used_mb_over_base_max"),
                    get(ph, "gpu", "util_pct_mean"),
                    sum(ph.get("undrained", {}).values()),
                ]
            )
    return rows


PHASE_HEADERS = [
    "config",
    "phase",
    "work items/s",
    "work p50 ms",
    "work p99 ms",
    "chain jobs/s",
    "dispatcher calls/s",
    "dispatcher busy",
    "dispatcher cpu",
    "process cores",
    "alloc max MB",
    "device MB over base",
    "GPU util %",
    "undrained",
]


def m1_section(doc: dict) -> list[str]:
    rows = []
    for c in doc["configs"]:
        for s in c.get("sizes", []):
            if s.get("error"):
                rows.append([c["name"], s["kind"], s["size"], "ERROR", *[None] * 8])
                continue

            rows.append(
                [
                    c["name"],
                    s["kind"],
                    s["size"],
                    get(s, "oneway", "latency_us", "p50"),
                    get(s, "oneway", "latency_us", "p99"),
                    get(s, "echo", "rtt_us", "p50"),
                    get(s, "echo", "rtt_us", "p99"),
                    get(s, "stream", "mb_per_s"),
                    get(s, "stream", "msgs_per_s"),
                    get(s, "oneway", "sender_cpu_us_per_msg"),
                    get(s, "oneway", "peer_cpu_us_per_msg"),
                    get(s, "stream", "sender_cpu_us_per_msg"),
                ]
            )

    headers = [
        "link",
        "kind",
        "bytes",
        "one-way p50 µs",
        "one-way p99 µs",
        "echo p50 µs",
        "echo p99 µs",
        "stream MB/s",
        "stream msg/s",
        "send CPU µs/msg",
        "peer CPU µs/msg",
        "stream send CPU µs/msg",
    ]
    return table(headers, rows)


def m3_section(doc: dict) -> list[str]:
    rows = []
    for c in doc["configs"]:
        for ph in c.get("phases", []):
            if ph.get("error"):
                rows.append([c["name"], ph["phase"], "ERROR", *[None] * 6])
                continue

            rows.append(
                [
                    c["name"],
                    ph["phase"],
                    get(ph, "feeders", "a", "items_ok_per_s"),
                    get(ph, "feeders", "b", "items_ok_per_s"),
                    get(ph, "feeders", "a", "latency_ms", "p50"),
                    get(ph, "feeders", "b", "latency_ms", "p50"),
                    get(ph, "probes", "gpu", "after", "max_allocated_mb"),
                    get(ph, "gpu", "used_mb_over_base_max"),
                    get(ph, "gpu", "util_pct_mean"),
                ]
            )

    out = table(
        [
            "layout",
            "phase",
            "a items/s",
            "b items/s",
            "a p50 ms",
            "b p50 ms",
            "alloc max MB",
            "device MB over base",
            "GPU util %",
        ],
        rows,
    )

    # Per layout: each side's rate with both running over its rate alone
    inter = [
        [
            c["name"],
            get(c, "interference", "a", "ratio"),
            get(c, "interference", "b", "ratio"),
            get(c, "gpu", "config", "used_mb_over_base_max"),
        ]
        for c in doc["configs"]
    ]
    out += ["Interference (items/s with both / alone):", ""]
    return out + table(["layout", "a ratio", "b ratio", "device MB over base (config)"], inter)


def extra_section(doc: dict, key: str, label: str) -> list[str]:
    rows = [
        [c["name"], ph["phase"], ph.get(key)]
        for c in doc["configs"]
        for ph in c.get("phases", [])
        if not ph.get("error")
    ]
    return table(["config", "phase", label], rows)


def failures(docs: dict[str, dict]) -> list[str]:
    out = []
    for name, doc in docs.items():
        for c in doc["configs"]:
            if c.get("error"):
                out.append(f"- {name} {c['name']}: {last_line(c['error'])}")
            out.extend(
                f"- {name} {c['name']} {ph['phase']}: {last_line(ph['error'])}"
                for ph in c.get("phases", [])
                if ph.get("error")
            )
            out.extend(
                f"- {name} {c['name']} {s['kind']} {s['size']}: {s['error'][:200]}"
                for s in c.get("sizes", [])
                if s.get("error")
            )
    return out or ["- none"]


def write(root: Path, names: list[str]) -> Path:
    env = json.loads((root / "env.json").read_text(encoding="utf-8"))
    docs = {}
    for n in names:
        path = root / f"{n}.json"
        if path.exists():
            docs[n] = json.loads(path.read_text(encoding="utf-8"))

    lines = [f"# Study S1, scale {env.get('scale', {}).get('name')}", ""]
    lines += [
        f"- python {env.get('python')}, torch {env.get('torch')}, " f"CUDA {env.get('cuda')}",
        f"- GPU {get(env, 'gpu', 'name')}, {fmt(get(env, 'gpu', 'total_mb'))} MB; "
        f"CPUs {env.get('cpu_count')}; {env.get('platform')}",
        f"- psutil {env.get('psutil')} (child-process CPU is reported only with psutil)",
        "",
    ]
    for n, doc in docs.items():
        lines += [f"## {n.upper()}: {doc['title']}", "", f"Wall time {fmt(doc['seconds'])} s.", ""]
        match n:
            case "m1":
                lines += m1_section(doc)
            case "m3":
                lines += m3_section(doc)
            case _:
                lines += table(PHASE_HEADERS, phase_rows(doc))
        match n:
            case "m4":
                lines += extra_section(doc, "cpu_us_per_item", "process CPU µs / item")
                solo = [[c["name"], get(c, "solo_ms", "p50")] for c in doc["configs"]]
                lines += table(["config", "one call on one thread, p50 ms"], solo)
            case "m5":
                lines += extra_section(doc, "client_cpu_us_per_request", "client CPU µs / request")
                lines += extra_section(
                    doc, "service_cpu_us_per_request", "service CPU µs / request"
                )
    lines += ["## Failures", "", *failures(docs), ""]

    out = root / "summary.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
