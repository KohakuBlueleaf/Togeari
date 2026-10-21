"""``summary.md`` of an S8 run: the rule and verdicts first, then every table."""

from p8.summary.decide import REFERENCE, RULE


def num(x: float | None, digits: int = 0) -> str:
    return "–" if x is None else f"{x:,.{digits}f}"


def cell(s: dict | None, digits: int = 0) -> str:
    """``median (min..max)``, or the median alone for one sample."""
    if not s:
        return "–"
    if s["n"] == 1:
        return num(s["median"], digits)
    return f"{num(s['median'], digits)} ({num(s['min'], digits)}..{num(s['max'], digits)})"


def ratio(mine: dict | None, ref: dict | None) -> str:
    return f"{mine['median'] / ref['median']:.2f}x" if mine and ref and ref["median"] else "–"


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return lines + ["| " + " | ".join(r) + " |" for r in rows] + [""]


def head(doc: dict) -> list[str]:
    env = doc["env"]
    variants = "; ".join(
        f"`{n}` = core {v['core']}, knobs {v['knobs']}" for n, v in doc["variants"].items()
    )
    parity = doc["parity"]
    return [
        f"# P8 S8: the cores measured ({doc['scale']})",
        "",
        f"Commit `{env['git_sha']}` ({len(env['git_dirty_files'] or [])} tracked files changed),"
        f" togeari {env['togeari']}, Python {env['python'].split()[0]}, {env['platform']},"
        f" {env['cpu_count']} CPUs, GPU 0: {env['gpu0']}. Variants: {variants}.",
        "",
        "## Decision rule",
        "",
        RULE,
        "",
        "## Verdicts",
        "",
        f"Parity: `{parity['command']}` {'passed' if parity['passed'] else 'FAILED'}"
        f" ({parity['tail']}).",
        "",
        *[f"- **{k}** = `{v['value']}`: {v['reason']}." for k, v in doc["verdicts"].items()],
        "",
        "## Failures",
        "",
        *failures(doc),
        "",
    ]


def failures(doc: dict) -> list[str]:
    """Every failure per variant (a failed variant cannot be chosen), or that there was none."""
    out = []
    if doc.get("moved_aside"):
        out.append(f"An earlier run's output was moved to `{doc['moved_aside']}` first.")
        out.append("")
    bad = {v: xs for v, xs in doc["failures"].items() if xs}
    if not bad:
        return out + ["None: every child exited 0 and every job was ok."]
    for v, xs in bad.items():
        out.append(f"- `{v}` ({len(xs)}): " + "; ".join(xs))
    return out


def against(doc: dict) -> list[str]:
    out = []
    for name, c in doc["compare"].items():
        out += [
            f"### {name}",
            "",
            f"{c['faster']} faster, {c['tie']} ties, {c['slower']} slower,"
            f" {c['missing']} missing.",
            "",
        ]
        for verdict in ("slower", "missing"):
            rows = [r for r in c["rows"] if r["verdict"] == verdict]
            if not rows:
                continue
            out += [f"Every {verdict} shape:", ""]
            out += table(
                ["shape", "ratio", "spread", c["baseline"], c["variant"]],
                [
                    [
                        r["measure"],
                        f"{r['ratio']:.3f}" if "ratio" in r else "–",
                        f"±{r['spread']:.1%}" if "spread" in r else "–",
                        cell(r["baseline"], 1),
                        cell(r["variant"], 1),
                    ]
                    for r in rows
                ],
            )
    return out


MARK = {"faster": "+", "slower": "−", "tie": "=", "missing": "?"}


def matrix(doc: dict) -> list[str]:
    """Per baseline, every shape's ratio and verdict for each variant compared with it."""
    out = []
    baselines = list(dict.fromkeys(c["baseline"] for c in doc["compare"].values()))
    for base in baselines:
        cs = [c for c in doc["compare"].values() if c["baseline"] == base]
        out += [
            f"### Every shape against {base}",
            "",
            "Ratio (above 1 is faster) and verdict: + faster, = tie, − slower, ? missing.",
            "",
        ]
        rows: dict[str, list[str]] = {}
        for i, c in enumerate(cs):
            for r in c["rows"]:
                cells = rows.setdefault(r["measure"], ["–"] * len(cs))
                ratio_s = f"{r['ratio']:.2f}" if "ratio" in r else "–"
                cells[i] = f"{ratio_s} {MARK[r['verdict']]}"
        out += table(["shape", *[c["variant"] for c in cs]], [[m, *v] for m, v in rows.items()])
    return out


def tables(doc: dict) -> list[str]:
    variants = list(doc["variants"])
    others = [v for v in variants if v != REFERENCE]
    vs_ref = [f"{v}/{REFERENCE}" for v in others]
    data = doc["data"]
    out = [
        "## Dispatcher ceiling",
        "",
        f"Calls/s, median (min..max) of {doc['rounds']}"
        " interleaved rounds; us/call from the median.",
        "",
    ]
    rows = []
    for shape, by_setting in data["ceiling"].items():
        for setting, by_v in by_setting.items():
            calls = {v: (by_v.get(v) or {}).get("calls") for v in variants}
            us = [num((by_v.get(v) or {}).get("us_per_call"), 1) for v in variants]
            rows.append(
                [shape, setting, *[cell(calls[v]) for v in variants], *us]
                + [ratio(calls[v], calls.get(REFERENCE)) for v in others]
            )
    out += table(
        ["shape", "setting", *[f"{v} calls/s" for v in variants]]
        + [f"{v} us/call" for v in variants]
        + vs_ref,
        rows,
    )

    out += [
        "## Scaling",
        "",
        "Us per job (resident) or per item (each), median (min..max) of"
        " the measured repeats over every round (a warm-up run discarded first); lower is"
        " better.",
        "",
    ]
    rows = [
        [shape, *[cell(by_v.get(v), 1) for v in variants]]
        + [ratio(by_v.get(v), by_v.get(REFERENCE)) for v in others]
        for shape, by_v in data["scaling"].items()
    ]
    out += table(["shape n", *variants, *vs_ref], rows)
    out += [
        "Regions and GIL offers of the Rust variants per job or item: offers (released"
        " regions / held regions), and offers/s, medians.",
        "",
    ]
    rows = []
    for shape, by_v in data.get("scaling_native", {}).items():
        cells = []
        for v in others:
            x = by_v.get(v) or {}
            med = {k: (x.get(k) or {}).get("median") for k in x}
            if med.get("offers_per_unit") is None:
                cells.append("–")
                continue
            cells.append(
                f"{num(med['offers_per_unit'], 2)} ({num(med['regions_released_per_unit'], 2)}"
                f" / {num(med['regions_held_per_unit'], 2)}), {num(med['offers_per_s'])}/s"
            )
        rows.append([shape, *cells])
    out += table(["shape n", *others], rows)

    for study, title in (("A", "P6 study A: event levels"), ("B", "P6 study B: knob defaults")):
        out += [
            f"## {title}",
            "",
            "Calls/s, median (min..max) over rounds (one round per child, every variant per"
            " round); dispatcher us/call median in brackets.",
            "",
        ]
        rows = []
        for name, by_v in data["p6"][study].items():
            cells = []
            for v in variants:
                x = by_v.get(v) or {}
                fail = f", {x['failed']} failed" if x.get("failed") else ""
                fail += f", {x['short']} jobs not ok" if x.get("short") else ""
                us = (x.get("dispatcher_us") or {}).get("median")
                cells.append(f"{cell(x.get('calls'))} [{num(us, 1)}]{fail}")
            ref = (by_v.get(REFERENCE) or {}).get("calls")
            rows.append(
                [name, *cells, *[ratio((by_v.get(v) or {}).get("calls"), ref) for v in others]]
            )
        out += table(["config", *variants, *vs_ref], rows)

    loads = doc["gil_loads"]
    out += [
        "## GIL contention",
        "",
        "Engine items/s fed directly, median (min..max) over rounds;"
        " share of its idle median; dispatcher calls/s and GIL offers/s (Rust core) medians in"
        " the same window.",
        "",
    ]
    for err in data["gil"]["errors"]:
        out += [f"- failed: {err}"]
    for key, by_load in data["gil"]["with_work"].items():
        out += [f"### {key}", ""]
        rows = []
        for v in variants:
            cells = []
            for load in loads:
                x = (by_load.get(load) or {}).get(v)
                if not x or not x["rate"]:
                    cells.append("failed")
                    continue
                share = f" {x['share']:.0%}" if x["share"] is not None else ""
                calls = (x["calls_per_s"] or {}).get("median")
                offers = (x.get("offers_per_s") or {}).get("median")
                shown = f", {num(offers)} offers/s" if offers is not None else ""
                cells.append(f"{cell(x['rate'])}{share}, {num(calls)} calls/s{shown}")
            rows.append([v, *cells])
        out += table(["variant", *loads], rows)
    out += [
        "### The dispatcher alone",
        "",
        "Calls/s median (min..max), busy median, GIL offers/s median (Rust core).",
        "",
    ]
    rows = []
    for key, by_load in data["gil"]["alone"].items():
        for v in variants:
            cells = []
            for load in loads[1:]:
                x = (by_load.get(load) or {}).get(v)
                if not x:
                    cells.append("–")
                    continue
                busy = (x.get("busy") or {}).get("median")
                offers = (x.get("offers_per_s") or {}).get("median")
                shown = f", {num(offers)} offers/s" if offers is not None else ""
                cells.append(f"{cell(x['calls_per_s'])} (busy {num(busy, 2)}){shown}")
            rows.append([key, v, *cells])
    out += table(["work / isolation", "variant", *loads[1:]], rows)
    return out


def markdown(doc: dict) -> str:
    steps = [
        [s["name"], str(s["exit_code"]), f"{s['seconds']:.1f}", f"`{s['log']}`"]
        for s in doc["steps"]
    ]
    out = head(doc) + ["## Every shape against its baseline", ""] + against(doc) + matrix(doc)
    out += tables(doc)
    out += ["## Runs", ""] + table(["step", "exit", "seconds", "log"], steps)
    return "\n".join(out)
