"""Programs and helpers shared by the plugin checks."""

import json
from pathlib import Path

PROGRAM = """
@def (a: int, b: int = 3)main(result):
    (a)inc(x)
    (x, b)mix(y)
    (y)small(c)
    (c)branch(`yes`, `no`)
    yes:
        (y)inc(z)
    no:
        z = 7
    (z, x)mix(result)
"""


def jobs(n: int) -> list[dict]:
    return [{"a": i} for i in range(n)]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
