"""Fault isolation and supervision: a broken part fails what it touches, never hangs a caller."""

import logging
import time

import pytest
from rt_helpers import INC, LIB, in_thread, machine, plugin, sim_lane

from togeari import ConstructionError, DispatcherDied, EngineBase, Machine, Use
from togeari.core.contracts.digest import digest_providers, register_digest, unregister_digest
from togeari.io import Iterable, Memory

logging.disable(logging.CRITICAL)


def test_dispatcher_death_answers_run_handles_and_later_calls():
    lane = sim_lane()
    m = machine(bind={"inc": Use(lane)})
    try:

        def broken(now):
            raise RuntimeError("lane bug")

        lane.pump = broken
        h = m.submit({"x": 1})
        finished, got = in_thread(
            lambda: m.run(Iterable([{"x": i} for i in range(50)]), Memory()), 5
        )
        assert finished, "run() hung after the dispatcher died"
        assert isinstance(got, DispatcherDied)
        assert h.result(5).failure["kind"] == "DispatcherDied"
        with pytest.raises(DispatcherDied):
            m.submit({"x": 2})
    finally:
        m.stop()


def test_a_post_result_returning_junk_disables_the_plugin_not_the_machine():
    p = plugin("junk", post_result=lambda result, **ctx: "junk")
    m = machine(plugins=[p])
    try:
        recs = [m.submit({"x": i}).result(5) for i in range(5)]
        assert all(r.ok and r.outputs == {"y": i + 1} for i, r in enumerate(recs))
        assert "junk" not in m.plugins.enabled
    finally:
        m.stop()


def test_plugin_errors_log_keeps_the_plugin():
    def boom(record):
        raise RuntimeError("x")

    p = plugin("noisy", on_job_end=boom)
    m = machine(plugins=[p], plugin_errors="log")
    try:
        assert m.submit({"x": 1}).result(5).ok
        assert "noisy" in m.plugins.enabled
        assert m.inspect("plugins").result()[0]["errors"] == 1
    finally:
        m.stop()


def test_a_raising_bus_listener_is_removed():
    m = machine()
    try:

        def listener(kind, fields):
            raise RuntimeError("listener bug")

        m.bus.listeners.append(listener)
        assert m.submit({"x": 1}).result(5).ok
        assert listener not in m.bus.listeners
    finally:
        m.stop()


def test_a_plugin_whose_on_load_raises_is_not_enabled():
    def bad(ctx):
        raise RuntimeError("no")

    p = plugin("badload", on_load=bad, on_job_end=lambda rec: None)
    m = machine(plugins=[p])
    try:
        assert "badload" not in m.plugins.enabled
        assert m.submit({"x": 1}).result(5).ok
    finally:
        m.stop()


def test_a_plugin_whose_on_load_fails_halfway_is_unloaded_once():
    # A plugin whose on_load raises gets one on_unload, which releases what it took
    held = []
    unloads = []

    def half(ctx):
        held.append("resource")
        raise RuntimeError("halfway")

    def release():
        unloads.append(held.pop())

    p = plugin("halfway", on_load=half, on_unload=release)
    m = machine(plugins=[p])
    try:
        assert m.submit({"x": 1}).result(5).ok
    finally:
        m.stop()
    assert unloads == ["resource"] and held == []


def test_digest_providers_name_the_registering_modules():
    register_digest(Marker, marker_digest)
    try:
        assert __name__ in digest_providers()
        assert digest_providers() == sorted(digest_providers())
    finally:
        unregister_digest(Marker)

    # Unregistering drops the module from the providers
    assert __name__ not in digest_providers()


class Marker:
    pass


def marker_digest(value):
    return b"marker"


def test_a_posted_call_that_raises_is_the_plugins_error():
    holder = {}
    p = plugin("poster", on_load=lambda ctx: holder.setdefault("ctx", ctx))
    m = machine(plugins=[p])
    try:
        assert m.submit({"x": 1}).result(5).ok

        def bad():
            raise RuntimeError("posted bug")

        assert holder["ctx"].post(bad)
        assert m.submit({"x": 2}).result(5).ok
        assert "poster" not in m.plugins.enabled
    finally:
        m.stop()


def test_a_failing_store_never_stops_the_machine():
    class Broken:
        def get(self, key):
            raise OSError("disk gone")

        def put(self, key, value):
            raise OSError("disk gone")

    m = machine(bind={"inc": Use(sim_lane(), store="content")}, store=Broken())
    try:
        assert m.submit({"x": 1}).result(5).outputs == {"y": 2}
        assert m.inspect("stats").result()["store_errors"] >= 2
    finally:
        m.stop()


def test_a_source_error_is_raised_after_the_run():
    def source():
        yield {"x": 1}
        raise ValueError("bad source")

    m = machine()
    try:
        sink = Memory()
        with pytest.raises(ValueError, match="bad source"):
            m.run(source(), sink)
        assert len(sink.records) == 1
    finally:
        m.stop()


def test_a_sink_error_is_raised_after_the_run():
    class BadSink(Memory):
        def write(self, record):
            raise OSError("sink full")

    m = machine()
    try:
        finished, got = in_thread(
            lambda: m.run(Iterable([{"x": i} for i in range(5)]), BadSink()), 5
        )
        assert finished and isinstance(got, OSError)
    finally:
        m.stop()


def test_the_sink_queue_is_bounded():
    seen = []

    class Slow(Memory):
        def write(self, record):
            seen.append(m.dispatcher.sink_q.qsize())
            time.sleep(0.002)
            super().write(record)

    m = machine(sink_buffer=4)
    try:
        sink = Slow()
        m.run(Iterable([{"x": i} for i in range(60)]), sink)
        assert len(sink.records) == 60
        assert max(seen) <= 4
    finally:
        m.stop()


class Hang(EngineBase):
    stopped = False

    def start(self):
        time.sleep(3600)

    def capacity(self):
        return 1

    def submit(self, work, done):
        return None

    def stop(self):
        Hang.stopped = True


class Fine(EngineBase):
    stopped = False

    def capacity(self):
        return 1

    def submit(self, work, done):
        return None

    def stop(self):
        Fine.stopped = True


def test_a_hung_start_is_bounded_and_started_units_are_stopped():
    def build():
        return Machine(
            program=INC.replace("(x)inc(y)", "(x)inc(a)\n    (a)ident(y)"),
            entry="main",
            libraries=[LIB],
            bind={"inc": Use(Hang(name="hang")), "ident": Use(Fine(name="fine"))},
            start_timeout_s=0.3,
        )

    finished, got = in_thread(build, 5)
    assert finished, "construction waited for a hung start()"
    assert isinstance(got, ConstructionError) and "timed out" in str(got)
    assert Fine.stopped
