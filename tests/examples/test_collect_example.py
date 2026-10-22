"""Run nested collect's standalone example on each runtime core."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("core", ["python", "rust"])
def test_collect_example(core):
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "collect" / "machine.py")],
        cwd=ROOT,
        env={**os.environ, "TOGEARI_CORE": core},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "conformance: 3/3 jobs match the reference" in result.stdout
    assert "[[2, 4], [], [6]]" in result.stdout
