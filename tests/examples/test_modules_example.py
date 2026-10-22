"""The generic imported-library example executes and conforms on both cores."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("core", ["python", "rust"])
def test_imported_library_example(core):
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples/modules/machine.py"), "--core", core],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{core} conformance: 3/3 replay + reexecute match reference" in result.stdout
    assert f"{core} origins: same definition node, two distinct call-site chains" in result.stdout
    assert '"module": "transforms.kir"' in result.stdout
    assert '"definition": "decorate"' in result.stdout
