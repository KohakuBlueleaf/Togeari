"""Writing a study's results: one JSON per configuration, a summary table."""

import json
import re
from pathlib import Path


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def write_config(root: Path, name: str, result: dict) -> Path:
    """``<root>/configs/<name>.json`` with every measured value."""
    out = root / "configs" / f"{slug(name)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, default=repr), encoding="utf-8")
    return out


def write_summary(root: Path, title: str, header: list[str], rows: list[list], notes: str) -> Path:
    """``<root>/summary.md``: the title, a table and notes."""
    lines = [f"# {title}", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    lines += ["", notes, ""]
    out = root / "summary.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _cell(value) -> str:
    match value:
        case None:
            return "–"
        case float():
            return f"{value:,.2f}" if abs(value) < 100 else f"{value:,.0f}"
        case int():
            return f"{value:,}"
        case _:
            return str(value)
