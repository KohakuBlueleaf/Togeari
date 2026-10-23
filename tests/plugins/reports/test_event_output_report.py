"""Event-output inspection is detached cached metadata, not sink/journal I/O."""

from types import SimpleNamespace

from togeari import Inline, Machine
from togeari.core.plugin.base import BasePlugin, PluginContext
from togeari.core.plugin.tables import q_event_output
from togeari.plugins.report.spec import machine_spec


class CachedManager:
    def __init__(self):
        self.config = {"enabled": True, "journal_identity": {"durable": False}, "max_events": 2}
        self.detail = {"enabled": True, "pending": [{"event_id": "a"}], "delivered": 0}
        self.seen = []

    def configuration(self):
        return self.config

    def status(self, jobuid=None):
        self.seen.append(jobuid)
        return self.detail


def test_inspector_event_output_copies_cached_manager_state():
    manager = CachedManager()
    row = q_event_output(SimpleNamespace(events=manager), "b" * 32)
    assert row["enabled"] and manager.seen == ["b" * 32]
    row["configuration"]["journal_identity"]["durable"] = True
    row["status"]["pending"].clear()
    assert not manager.config["journal_identity"]["durable"]
    assert manager.detail["pending"]


def test_report_event_output_is_additive_and_disabled_without_configuration():
    machine = Machine(
        program="(1)neg(result)\n", bind={"neg": Inline()}, core="python", start_units=False
    )
    try:
        ctx = PluginContext(machine, BasePlugin())
        before = machine_spec(ctx)
        assert not before["event_output"]["enabled"]
        manager = machine.events
        machine.events = CachedManager()
        try:
            report = machine_spec(ctx)
            assert report["event_output"]["enabled"]
            assert report["programs"][0]["semantic_hash"] == before["programs"][0]["semantic_hash"]
            assert report["event_output"]["configuration"]["journal_identity"] == {"durable": False}
        finally:
            machine.events = manager
    finally:
        machine.stop()
