"""Core selection (spec/11 §11.2): ``core=``, then ``TOGEARI_CORE``, then ``"auto"``; asking
for a core that cannot run is a construction error naming where the ask came from."""

import io

import pytest
from kohakunode.testing import LIB

from togeari import ConstructionError, Inline, Machine
from togeari.cli.commands import doctor
from togeari.core.construct import core
from togeari.core.construct.knobs import KNOB_DEFAULTS

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
def without_dispatcher(monkeypatch):
    """An extension built without the native dispatcher, for the selection rules alone."""

    class Native:
        __version__ = "0.0.0"
        DISPATCHER = False

    monkeypatch.setattr(core, "_rs", Native)


@pytest.fixture
def auto_python(monkeypatch):
    """`"auto"` set to the Python core, the other value `AUTO` can take."""
    monkeypatch.setattr(core, "AUTO", "python")


def test_the_extension_is_built_and_holds_the_dispatcher():
    rs = core.native()
    assert rs is not None
    assert rs.DISPATCHER is True
    info = rs.core_info()
    assert (info["version"], info["dispatcher"]) == (rs.__version__, True)
    assert isinstance(info["debug"], bool)
    assert core.rust_available() is True


def test_auto_and_the_offer_knob_are_the_measured_choices():
    """spec/11 §11.6 chose the Rust core with ``gil_offer_us=None``."""
    assert core.AUTO == "rust"
    assert KNOB_DEFAULTS["gil_offer_us"] is None


@pytest.mark.parametrize(
    ("asked", "resolved"), [(None, "rust"), ("auto", "rust"), ("python", "python")]
)
def test_auto_runs_rust_and_python_runs_python(no_env, asked, resolved):
    m = machine(core=asked)
    try:
        assert m.core == resolved
        native = type(m.dispatcher).__name__ == "NativeDispatcher"
        assert native is (resolved == "rust")
    finally:
        m.stop()


@pytest.mark.parametrize("asked", [None, "auto"])
def test_auto_without_the_extension_runs_python(no_env, monkeypatch, asked):
    monkeypatch.setattr(core, "_rs", None)
    m = machine(core=asked)
    try:
        assert m.core == "python"
        assert type(m.dispatcher).__name__ != "NativeDispatcher"
        assert m.submit({"a": 1}).result(10).outputs == {"b": 2}
    finally:
        m.stop()


def test_auto_without_the_dispatcher_runs_python(no_env, without_dispatcher):
    m = machine()
    try:
        assert m.core == "python"
    finally:
        m.stop()


def test_rust_runs_the_native_dispatcher(no_env):
    m = machine(core="rust")
    try:
        assert m.core == "rust"
        assert type(m.dispatcher).__name__ == "NativeDispatcher"
        assert m.submit({"a": 1}).result(10).outputs == {"b": 2}
    finally:
        m.stop()


def test_rust_without_the_dispatcher_is_a_construction_error(no_env, without_dispatcher):
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
    m = machine()
    try:
        assert m.core == "rust"
    finally:
        m.stop()

    monkeypatch.setattr(core, "_rs", None)
    with pytest.raises(ConstructionError) as err:
        machine()
    assert 'TOGEARI_CORE="rust"' in str(err.value)
    monkeypatch.undo()

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


def test_the_rules_with_the_dispatcher(no_env, monkeypatch):
    assert core.resolve(None) == (core.AUTO, None)
    assert core.resolve("auto") == (core.AUTO, None)
    assert core.resolve("rust") == ("rust", None)
    assert core.resolve("python") == ("python", None)
    monkeypatch.setenv(core.ENV, "python")
    assert core.resolve(None) == ("python", None)


def test_auto_takes_rust_only_when_built(no_env, monkeypatch):
    assert core.resolve("auto") == ("rust", None)
    monkeypatch.setattr(core, "_rs", None)
    assert core.resolve("auto") == ("python", None)


def test_auto_follows_its_setting(no_env, auto_python):
    assert core.resolve("auto") == ("python", None)
    assert core.resolve("rust") == ("rust", None)


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
    assert line.startswith(f"core     {core.AUTO}  (auto; togeari._rs ")
    assert "dispatcher yes" in line
