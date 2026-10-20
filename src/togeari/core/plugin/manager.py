"""The plugin manager: priority order, hook semantics, per-hook lists, enable / disable, errors.

A plugin error (a hook that raises, or returns a value of the wrong type) is
logged and its change skipped; with the ``plugin_errors`` knob at "disable"
the plugin is also disabled.
"""

import logging
import time
from typing import Any

from togeari.core.plugin.base import BasePlugin, PluginBlockError, PluginContext
from togeari.core.policies.former import Greedy
from togeari.core.policies.select import LeastLoaded

log = logging.getLogger("togeari.plugin")

HOOKS = (
    "pre_admit",
    "vote_admit",
    "on_job_start",
    "on_job_end",
    "pre_submit",
    "post_result",
    "on_unit_state",
    "on_fault",
    "pre_sink",
    "on_machine_start",
    "on_machine_stop",
)
GATES = (("entries", "entry"), ("lanes", "lane"))


def overrides(plugin: Any, hook: str) -> bool:
    impl = getattr(type(plugin), hook, None)
    if impl is None:
        return False
    base = getattr(BasePlugin, hook, None)
    return base is None or impl is not base


class PluginManager:
    def __init__(self, machine: Any):
        self.m = machine
        self.plugins: list[BasePlugin] = []
        self.enabled: set[str] = set()
        self.contexts: dict[str, PluginContext] = {}
        self.by_hook: dict[str, list[BasePlugin]] = {h: [] for h in HOOKS}
        self.timing: dict[str, dict[str, float]] = {}
        self.errors: dict[str, int] = {}

        # What each plugin put into the machine's tables: (table, key, value)
        self.contributed: dict[str, list[tuple[dict, Any, Any]]] = {}

    # ------------------------------------------------------------ registry
    def add(self, plugin: BasePlugin) -> None:
        name = getattr(plugin, "name", type(plugin).__name__)
        if any(p.name == name for p in self.plugins):
            name = f"{name}#{len(self.plugins)}"
        plugin.name = name
        self.plugins.append(plugin)
        self.plugins.sort(key=lambda p: getattr(p, "priority", 50))
        self.contexts[name] = PluginContext(self.m, plugin)

    def collect_policies(self) -> dict:
        """Every plugin's ``contribute_policies()``, by name (read before programs are bound)."""
        out: dict = {}
        for p in self.plugins:
            got = self._policies(p)
            out.update(got)
            self.contributed.setdefault(p.name, []).extend(
                (out, key, value) for key, value in got.items()
            )
        return out

    def _policies(self, p: BasePlugin) -> dict:
        """The plugin's ``contribute_policies()``; empty when it has none or it failed."""
        if getattr(p, "contribute_policies", None) is None:
            return {}
        ok, got = self.call(p, "contribute_policies")
        if ok and isinstance(got, dict):
            return got
        if ok and got is not None:
            self.failed(p, "contribute_policies", TypeError(f"returned {type(got).__name__}"))
        return {}

    def load(self, plugin: BasePlugin) -> bool:
        """``on_load``, contributions, the bus subscription; False when ``on_load`` failed (after
        the plugin's ``on_unload``)."""
        ok, _ = self.call(plugin, "on_load", self.contexts[plugin.name])
        if not ok:
            # What on_load took before it failed is released, and its policies withdrawn
            log.error("plugin %s not enabled: on_load failed", plugin.name)
            self.call(plugin, "on_unload")
            self.withdraw(plugin.name)
            return False
        self.enabled.add(plugin.name)

        # Contributions: queries, commands, services; each recorded for disable
        added = self.contributed.setdefault(plugin.name, [])
        for hook, table in (
            ("contribute_queries", self.m.queries),
            ("contribute_commands", self.m.commands),
            ("runtime_services", self.m.services),
        ):
            if getattr(plugin, hook, None) is None:
                continue
            ok, got = self.call(plugin, hook)
            if ok and isinstance(got, dict):
                table.update(got)
                added.extend((table, key, value) for key, value in got.items())

        # Policies again on a re-enable (the first ones are collected before binding)
        policies = getattr(self.m, "contributed_policies", None)
        if policies is not None and not any(t is policies for t, _, _ in added):
            got = self._policies(plugin)
            policies.update(got)
            added.extend((policies, key, value) for key, value in got.items())
        if getattr(plugin, "events", None) and hasattr(plugin, "on_event"):
            self.m.bus.subscribe(plugin)
        self.rebuild()
        return True

    def load_all(self) -> None:
        for p in list(self.plugins):
            self.load(p)

    def enable(self, name: str) -> bool:
        p = self.get(name)
        if p is None or name in self.enabled:
            return False
        return self.load(p)

    def disable(self, name: str) -> bool:
        """Disable a plugin without waiting on its event thread: its queued events are dropped."""
        p = self.get(name)
        if p is None or name not in self.enabled:
            return False
        self.enabled.discard(name)
        self.rebuild()
        self.withdraw(name)
        self.m.bus.unsubscribe(p, wait=False)
        self.call(p, "on_unload")
        return True

    def withdraw(self, name: str) -> None:
        """Remove what the plugin contributed: entries another plugin replaced since stay. A
        lane still using a withdrawn policy goes back to its default (``LeastLoaded`` unit
        choice; ``Greedy`` forming on a batch lane)."""
        withdrawn = []
        for table, key, value in self.contributed.pop(name, []):
            withdrawn.append(value)
            if table.get(key) is value:
                del table[key]

        # Lanes bound to a withdrawn former or selector
        for lane in getattr(self.m, "lanes", ()):
            if any(lane.former is v for v in withdrawn):
                log.warning("lane %s: former of plugin %s withdrawn; Greedy used", lane.name, name)
                lane.former = Greedy() if lane.kind == "batch" else None
            if any(lane.select is v for v in withdrawn):
                log.warning(
                    "lane %s: selector of plugin %s withdrawn; LeastLoaded used", lane.name, name
                )
                lane.select = LeastLoaded()

    def get(self, name: str) -> BasePlugin | None:
        return next((p for p in self.plugins if p.name == name), None)

    def rebuild(self) -> None:
        """Per-hook lists of enabled plugins that override the hook."""
        for h in HOOKS:
            self.by_hook[h] = [
                p for p in self.plugins if p.name in self.enabled and overrides(p, h)
            ]

    def unload_all(self) -> None:
        """Unload every enabled plugin; one with ``close_s`` gets one deadline for the delivery
        of its queued events and its own drain."""
        for p in self.plugins:
            if p.name not in self.enabled:
                continue
            close_s = getattr(p, "close_s", None)
            deadline = None if close_s is None else time.monotonic() + close_s
            p.unload_deadline = deadline
            try:
                self.m.bus.unsubscribe(p, deadline=deadline)
                self.call(p, "on_unload")
            finally:
                p.unload_deadline = None
        self.enabled.clear()
        self.rebuild()

    # ------------------------------------------------------------ errors, gating, timing
    def failed(self, p: BasePlugin, hook: str, exc: BaseException) -> None:
        self.errors[p.name] = self.errors.get(p.name, 0) + 1
        log.warning("plugin %s.%s failed", p.name, hook, exc_info=exc)
        if self.m.bus.wants("plugin.error"):
            self.m.bus.emit(
                "plugin.error", plugin=p.name, hook=hook, error=f"{type(exc).__name__}: {exc}"
            )
        if self.m.knobs["plugin_errors"] == "disable" and p.name in self.enabled:
            log.warning("plugin %s disabled after its error", p.name)
            self.disable(p.name)
            self.m.bus.emit(
                "plugin.disabled", plugin=p.name, hook=hook, error=f"{type(exc).__name__}: {exc}"
            )

    def failed_by_name(self, name: str, hook: str, exc: BaseException) -> None:
        p = self.get(name)
        if p is not None:
            self.failed(p, hook, exc)
        else:
            log.warning("posted call of %s failed", name, exc_info=exc)

    def applies(self, p: BasePlugin, where: dict | None) -> bool:
        gate = getattr(p, "applies_to", None)
        if gate and where:
            for key, field in GATES:
                value = where.get(field)
                if key in gate and value is not None and value not in gate[key]:
                    return False
        if type(p).should_apply is BasePlugin.should_apply:
            return True
        ok, got = self.call(p, "should_apply", self.contexts[p.name])
        return ok and bool(got)

    def _time(self, p: BasePlugin, hook: str, t0: float, blocked: bool = False) -> None:
        dt = time.perf_counter() - t0
        d = self.timing.setdefault(p.name, {})
        d[hook] = d.get(hook, 0.0) + dt
        if hook in HOOKS and self.m.bus.wants("plugin.hook"):
            self.m.bus.emit("plugin.hook", plugin=p.name, hook=hook, time_s=dt, blocked=blocked)

    def call(self, p: BasePlugin, hook: str, *args: Any, **kw: Any) -> tuple[bool, Any]:
        """(True, the hook's return value), or (False, None) after a plugin error."""
        fn = getattr(p, hook, None)
        if fn is None:
            return True, None
        t0 = time.perf_counter()
        try:
            return True, fn(*args, **kw)
        except Exception as exc:  # noqa: BLE001
            self.failed(p, hook, exc)
            return False, None
        finally:
            self._time(p, hook, t0)

    # ------------------------------------------------------------ hook kinds
    def transform(
        self, hook: str, value: Any, kind: Any = None, where: dict | None = None, **ctx: Any
    ) -> Any:
        """Chain pre/post hooks; ``PluginBlockError`` propagates; a non-``kind`` value errs."""
        for p in self.by_hook.get(hook, ()):
            if not self.applies(p, where):
                continue
            t0 = time.perf_counter()
            try:
                new = getattr(p, hook)(value, **ctx)
            except PluginBlockError:
                self._time(p, hook, t0, blocked=True)
                raise
            except Exception as exc:  # noqa: BLE001
                self._time(p, hook, t0)
                self.failed(p, hook, exc)
                continue
            self._time(p, hook, t0)
            if new is None:
                continue
            if kind is not None and not isinstance(new, kind):
                self.failed(p, hook, TypeError(f"returned {type(new).__name__}"))
                continue
            value = new
        return value

    def vote(self, hook: str, *args: Any, where: dict | None = None) -> bool:
        for p in self.by_hook.get(hook, ()):
            if not self.applies(p, where):
                continue
            ok, got = self.call(p, hook, *args)
            if ok and got is False:
                return False
        return True

    def notify(self, hook: str, *args: Any, where: dict | None = None) -> None:
        for p in self.by_hook.get(hook, ()):
            if self.applies(p, where):
                self.call(p, hook, *args)

    def has(self, hook: str) -> bool:
        return bool(self.by_hook.get(hook))


__all__ = ["HOOKS", "PluginBlockError", "PluginManager"]
