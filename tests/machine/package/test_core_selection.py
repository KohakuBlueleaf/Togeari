"""Core selection (spec/11 §11.2): ``core=``, then ``TOGEARI_CORE``, then ``"auto"``; asking
for a core that cannot run is a construction error naming where the ask came from."""

import io

import pytest
from kohakunode.testing import LIB

from togeari import ConstructionError, Inline, Machine
from togeari.cli.commands import doctor
from togeari.core.construct import core

PROGRAM = """
@def (a)main(b):
    (a)inc(b)
"""


def machine(**kw) -> Machine:
    return Machine(program=PROGRAM, entry="main", libraries=[LIB], bind={"*": Inline()}, **kw)


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv(core.ENV, raising=False)


@pytest.fixture
def with_dispatcher(monkeypatch):
    """An extension that holds the native dispatcher, for the selection rules alone."""

    class Native:
        __version__ = "0.0.0"
        DISPATCHER = True

    monkeypatch.setattr(core, "_rs", Native)


def test_the_extension_is_built_and_holds_no_dispatcher_yet():
    rs = core.native()
    assert rs is not None
    assert rs.DISPATCHER is False
    info = rs.core_info()
    assert (info["version"], info["dispatcher"]) == (rs.__version__, False)
    assert isinstance(info["debug"], bool)
    assert core.rust_available() is False


@pytest.mark.parametrize(
    ("asked", "resolved"), [(None, "python"), ("auto", "python"), ("python", "python")]
)
def test_auto_and_python_run_the_python_core(no_env, asked, resolved):
    m = machine(core=asked)
    try:
        assert m.core == resolved
    finally:
        m.stop()


def test_rust_without_the_dispatcher_is_a_construction_error(no_env):
    with pytest.raises(ConstructionError) as err:
        machine(core="rust")
    assert 'core="rust"' in str(err.value)
    assert "has no dispatcher" in str(err.value)


def test_an_unknown_core_is_a_construction_error(no_env):
    with pytest.raises(ConstructionError) as err:
        machine(core="fast")
    assert "core='fast' is not one of auto, rust, python" in str(err.value)


def test_the_environment_sets_the_default(monkeypatch):
    monkeypatch.setenv(core.ENV, "rust")
    with pytest.raises(ConstructionError) as err:
        machine()
    assert 'TOGEARI_CORE="rust"' in str(err.value)

    monkeypatch.setenv(core.ENV, "python")
    m = machine()
    try:
        assert m.core == "python"
    finally:
        m.stop()


def test_core_overrides_the_environment(monkeypatch):
    monkeypatch.setenv(core.ENV, "rust")
    m = machine(core="python")
    try:
        assert m.core == "python"
    finally:
        m.stop()


def test_the_rules_with_a_dispatcher(no_env, with_dispatcher, monkeypatch):
    assert core.resolve(None) == ("rust", None)
    assert core.resolve("auto") == ("rust", None)
    assert core.resolve("rust") == ("rust", None)
    assert core.resolve("python") == ("python", None)
    monkeypatch.setenv(core.ENV, "python")
    assert core.resolve(None) == ("python", None)


def test_the_rules_without_the_extension(no_env, monkeypatch):
    monkeypatch.setattr(core, "_rs", None)
    assert core.resolve("auto") == ("python", None)
    resolved, problem = core.resolve("rust")
    assert resolved == "python"
    assert problem == 'core="rust" but togeari._rs is not built'
    assert core.info()["extension"] is None


def test_doctor_reports_the_core(no_env, monkeypatch):
    monkeypatch.setattr(doctor, "devices", lambda timeout=120.0: {"torch": None})
    monkeypatch.setattr(doctor, "processes", lambda: None)
    out = io.StringIO()
    doctor.run(None, out)
    line = next(ln for ln in out.getvalue().splitlines() if ln.startswith("core"))
    assert line.startswith("core     python  (auto; togeari._rs ")
    assert "dispatcher no" in line
