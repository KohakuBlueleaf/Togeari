"""``togeari.__version__`` is the installed distribution's version, which is pyproject's."""

import importlib.metadata
import importlib.util
import re
from pathlib import Path

import togeari

ROOT = Path(__file__).resolve().parents[3]


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', text, re.MULTILINE).group(1)


def test_the_version_agrees_with_pyproject():
    assert togeari.__version__ == pyproject_version()


def test_the_version_comes_from_the_distribution(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: f"{name}-from-metadata")

    # The package's own __init__, run again as a separate module
    spec = importlib.util.spec_from_file_location("togeari_probe", togeari.__file__)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.__version__ == "togeari-from-metadata"
