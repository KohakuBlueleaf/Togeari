"""The decision rule (``RULE``, printed in the summary): every expected shape, one variant
against a baseline variant, and the ``gil_offer_us`` and ``AUTO`` verdicts it gives."""

from p8.variants import VARIANTS

TIE = 0.03
# Ratios within this of the tie line count as on it (a ratio of exactly 1.03 is a tie)
EPS = 1e-9
REFERENCE = "python"
FIRST_RUST = "rust-o0"

# The tie-break: engine items/s of this GIL study row at this load
TIE_BREAK = ("torch / process", "ceiling")

RULE = (
    "For every shape the run plans (independent of what it produced): ratio = variant median /"
    " baseline median (baseline / variant for times, so above 1 is always faster); spread = the"
    " sum of both medians' relative half-ranges (max - min) / 2 / median over the shape's"
    " samples (rounds, or repeats over the rounds). A shape is a tie when |ratio - 1| <="
    f" max(spread, {TIE:.0%}) (up to {EPS:g}), else faster or slower; a shape without both"
    " medians is missing. Shapes bound by simulated latency come out as ties by construction"
    " (both cores wait the same simulated time). The idle GIL load (no dispatcher work) is left"
    " out. A variant fails when any of its children did not exit 0 or timed out, a ceiling or"
    " P6 job was not ok, a P6 round raised, or a GIL config or phase failed (or any job in it);"
    " a failed variant cannot be chosen, and a failed `python` leaves `AUTO` python. The default"
    " `gil_offer_us` is the value of the Rust variant that did not fail and has 0 slower and 0"
    " missing shapes against `python`, with the most faster shapes against it; a tie is broken"
    " by the higher median torch / process engine items/s at the ceiling, then by the sweep order"
    " (0, 20, 100, 300, 1000, 5000, inf, None). `AUTO` = rust only when that chosen variant"
    " exists (never slower than python beyond noise) and every parity check passed; else `AUTO`"
    " stays python and `gil_offer_us` keeps 0."
)


def _half(s: dict | None) -> float:
    return (s["max"] - s["min"]) / 2 / s["median"] if s and s["median"] else 0.0


def versus(measure: str, base: dict | None, mine: dict | None, higher_better: bool) -> dict:
    row = {"measure": measure, "baseline": base, "variant": mine}
    if not base or not mine or not base["median"] or not mine["median"]:
        return {**row, "verdict": "missing"}
    ratio = mine["median"] / base["median"]
    if not higher_better:
        ratio = 1 / ratio
    spread = _half(base) + _half(mine)
    verdict = "faster" if ratio > 1 else "slower"
    if abs(ratio - 1) <= max(spread, TIE) + EPS:
        verdict = "tie"
    return {**row, "ratio": ratio, "spread": spread, "verdict": verdict}


def shapes(data: dict, expected: dict, base: str, v: str):
    """Every expected shape: (measure, baseline stat, variant stat, higher is better); a shape
    the run did not produce gives None stats (missing)."""

    def get(by_v: dict | None, x: str, field: str | None) -> dict | None:
        got = (by_v or {}).get(x)
        return (got or {}).get(field) if field else got

    for shape, setting in expected["ceiling"]:
        by_v = (data["ceiling"].get(shape) or {}).get(setting)
        b, m = (get(by_v, x, "calls") for x in (base, v))
        yield f"ceiling {shape} ({setting}) calls/s", b, m, True
    for shape in expected["scaling"]:
        by_v = data["scaling"].get(shape)
        yield f"scaling {shape} us", get(by_v, base, None), get(by_v, v, None), False
    for study, names in expected["p6"].items():
        for name in names:
            by_v = data["p6"][study].get(name)
            b, m = (get(by_v, x, "calls") for x in (base, v))
            yield f"P6 {study} {name} calls/s", b, m, True
    for key, load in expected["gil"]:
        by_v = (data["gil"]["with_work"].get(key) or {}).get(load)
        b, m = (get(by_v, x, "rate") for x in (base, v))
        yield f"GIL {key} engine items/s at {load}", b, m, True


def compare(data: dict, expected: dict, pairs: list[tuple[str, str]]) -> dict:
    """Per ``(variant, baseline)``: every expected shape's row and the counts per verdict."""
    out = {}
    for v, base in pairs:
        rows = [versus(*s) for s in shapes(data, expected, base, v)]
        counts = {k: sum(r["verdict"] == k for r in rows) for k in ("faster", "tie", "slower")}
        counts["missing"] = sum(r["verdict"] == "missing" for r in rows)
        out[f"{v} vs {base}"] = {"variant": v, "baseline": base, "rows": rows, **counts}
    return out


def pairs_of(variants: list[str]) -> list[tuple[str, str]]:
    """Each Rust variant against ``python``, and each other Rust variant against ``rust-o0``."""
    rust = [v for v in variants if v != REFERENCE]
    pairs = [(v, REFERENCE) for v in rust if REFERENCE in variants]
    if FIRST_RUST in variants:
        pairs += [(v, FIRST_RUST) for v in rust if v != FIRST_RUST]
    return pairs


def _counts(c: dict) -> str:
    return f"{c['faster']} faster, {c['tie']} ties, {c['slower']} slower, {c['missing']} missing"


def tie_break(data: dict, v: str) -> float:
    """The tie-break measure of variant ``v``: its median engine items/s (0 when missing)."""
    key, load = TIE_BREAK
    x = (((data["gil"]["with_work"].get(key) or {}).get(load) or {}).get(v) or {}).get("rate")
    return x["median"] if x else 0.0


def verdicts(compared: dict, parity: dict, data: dict, failed: dict[str, list]) -> dict:
    """The ``gil_offer_us`` default and ``AUTO``, each with its reason."""
    vs_ref = {c["variant"]: c for c in compared.values() if c["baseline"] == REFERENCE}
    parity_s = "passed" if parity["passed"] else "FAILED"
    if not vs_ref:
        reason = "no python variant measured, so nothing to compare with; 0 kept"
        return {
            "gil_offer_us": {"value": 0, "reason": reason},
            "AUTO": {"value": "python", "reason": f"no python baseline; parity {parity_s}"},
        }

    # Sorted with the sweep order kept for equal keys (a reverse sort in Python is stable)
    bad = {v for v, xs in failed.items() if xs}
    clean = [
        v
        for v, c in vs_ref.items()
        if c["slower"] == 0 and c["missing"] == 0 and v not in bad and REFERENCE not in bad
    ]
    ranked = sorted(clean, key=lambda v: (vs_ref[v]["faster"], tie_break(data, v)), reverse=True)
    chosen = ranked[0] if ranked else None
    every = "; ".join(
        f"{v}: {_counts(c)}{', FAILED' if v in bad else ''}" for v, c in vs_ref.items()
    )
    failed_s = ", ".join(sorted(bad)) or "none"

    out = {}
    if chosen is None:
        out["gil_offer_us"] = {
            "value": 0,
            "reason": (
                "no Rust variant without failures has 0 slower and 0 missing shapes against"
                f" python, 0 kept (failed: {failed_s}; {every})"
            ),
        }
    else:
        key, load = TIE_BREAK
        out["gil_offer_us"] = {
            "value": VARIANTS[chosen]["knobs"]["gil_offer_us"],
            "reason": (
                f"{chosen}: {_counts(vs_ref[chosen])}, {key} at {load}"
                f" {tie_break(data, chosen):,.0f} items/s (failed: {failed_s}; every variant:"
                f" {every})"
            ),
        }
    ok = chosen is not None and parity["passed"]
    out["AUTO"] = {
        "value": "rust" if ok else "python",
        "reason": f"chosen variant {chosen or 'none'}; failed: {failed_s}; parity {parity_s}",
    }
    return out
