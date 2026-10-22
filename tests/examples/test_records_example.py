"""The records example is standalone on either core and checks every job itself."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("core", ["python", "rust"])
def test_records_example(core):
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "records" / "machine.py")],
        cwd=ROOT,
        env={**os.environ, "TOGEARI_CORE": core},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "conformance: 3/3 jobs match the reference" in result.stdout
    assert "0: 1 -> 11" in result.stdout
    assert "1: -2 -> 3" in result.stdout
