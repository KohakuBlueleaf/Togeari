"""``togeari.__version__`` is the installed distribution's version, which is Cargo's, in
PEP 440 form; the native extension carries the same version."""

import importlib.metadata
import importlib.util
import re
from pathlib import Path

import togeari
from togeari.core.construct.core import native

ROOT = Path(__file__).resolve().parents[3]

# Cargo pre-release labels and their PEP 440 forms (maturin's mapping)
PRE = {"dev": ".dev", "alpha": "a", "a": "a", "beta": "b", "b": "b", "rc": "rc"}


def cargo_version() -> str:
    text = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', text, re.MULTILINE).group(1)


def pep440(semver: str) -> str:
    release, _, pre = semver.partition("-")
    if not pre:
        return release
    label, number = re.fullmatch(r"([a-z]+)\.?(\d*)", pre).groups()
    return f"{release}{PRE[label]}{number or 0}"


def test_the_pep440_mapping():
    assert pep440("0.2.0") == "0.2.0"
    assert pep440("0.2.0-dev0") == "0.2.0.dev0"
    assert pep440("0.2.0-alpha.1") == "0.2.0a1"
    assert pep440("1.0.0-rc.2") == "1.0.0rc2"


def test_the_version_agrees_with_cargo():
    assert togeari.__version__ == pep440(cargo_version())


def test_the_extension_carries_the_crate_version():
    # Every install (wheel or editable) builds the extension
    rs = native()
    assert rs is not None
    assert rs.__version__ == cargo_version()
    assert rs.core_info()["version"] == cargo_version()


def test_the_version_comes_from_the_distribution(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: f"{name}-from-metadata")

    # The package's own __init__, run again as a separate module
    spec = importlib.util.spec_from_file_location("togeari_probe", togeari.__file__)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.__version__ == "togeari-from-metadata"
