"""Machine: build every part from instances, check everything at once, run jobs (spec/06)."""

import contextvars
import threading
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from togeari.core.construct.core import resolve as resolve_core
from togeari.core.construct.errors import ConstructionError, Problems
from togeari.core.construct.knobs import KNOB_DEFAULTS, checked
from togeari.core.construct.machine.loading import LoadStage
from togeari.core.construct.machine.running import RunStage
from togeari.core.construct.machine.units import UnitStage, start_units_of
from togeari.core.construct.units import stop_all
from togeari.core.contracts.adapter import PythonAdapter
from togeari.core.lanes.lane import Lane
from togeari.core.observe.bus import Bus
from togeari.core.plugin.manager import PluginManager
from togeari.core.plugin.tables import COMMANDS, QUERIES
from togeari.core.policies.admission import Fixed
from togeari.core.policies.issue import OldestJobFirst
from togeari.core.program.bind import Defaults, Use
from togeari.core.program.code import CodeMemory
from togeari.core.runtime.dispatcher import Dispatcher

_NO_START = contextvars.ContextVar("togeari_no_unit_start", default=False)


@contextmanager
def no_unit_start():
    """Machines built inside this block check everything but start no unit; they cannot run."""
    token = _NO_START.set(True)
    try:
        yield
    finally:
        _NO_START.reset(token)


class Machine(LoadStage, UnitStage, RunStage):
    """Programs in code memory, lanes of units, plugins, policies and knobs, one dispatcher.

    Construction checks everything and raises one ``ConstructionError`` listing
    every problem; then every unit starts (none inside ``no_unit_start()``).
    """

    def __init__(
        self,
        programs: dict | None = None,
        program: Any = None,
        entry: str | None = None,
        libraries: list | None = None,
        sig_path: list[Path] | None = None,
        passes: list[str] | None = None,
        bind: dict | None = None,
        admission: Any = None,
        issue: Any = None,
        store: Any = None,
        plugins: list | None = None,
        adapter: Any = None,
        protocol: str = "python",
        start_timeout_s: float = 120.0,
        start_units: bool = True,
        source_buffer: int = 1024,
        sink_buffer: int = 1024,
        seed: int = 0,
        core: str | None = None,
        **knobs: Any,
    ):
        problems = Problems()

        # The core that runs this machine: core=, else TOGEARI_CORE, else "auto"
        self.core, problem = resolve_core(core)
        if problem is not None:
            problems.add("core", problem)

        self.libraries = list(libraries or [])
        self.sig_path = sig_path
        self.passes = passes
        self.bind = dict(bind or {})
        self.store = store
        self.source_buffer = source_buffer
        self.sink_buffer = sink_buffer
        self.start_timeout_s = start_timeout_s
        self.defaults = Defaults(adapter if adapter is not None else PythonAdapter(), protocol)
        self.units_started = False
        self.dispatcher: Dispatcher | None = None
        self._stopped = False
        self._run_lock = threading.Lock()

        # Loads in progress: the program hashes and entry names they stage
        self._loading: dict[str, Future] = {}
        self._loading_names: dict[str, Future] = {}

        # Plugins, and the policies they contribute to bindings and knobs
        self.knobs: dict[str, Any] = dict(KNOB_DEFAULTS)
        self.bus = Bus(self.knobs)
        self.queries: dict = dict(QUERIES)
        self.commands: dict = dict(COMMANDS)
        self.services: dict = {}
        self.plugins = PluginManager(self)
        for p in plugins or []:
            self.plugins.add(p)
        self.contributed_policies: dict = self.plugins.collect_policies()

        # Knob / policy registry
        given = {**knobs, "admission": admission or Fixed(64), "issue": issue or OldestJobFirst()}
        for name, value in given.items():
            value, problem = checked(name, value, self.contributed_policies)
            if problem is not None:
                problems.add("knobs", problem)
            else:
                self.knobs[name] = value

        # Store modes and snapshots need a machine store
        if store is None:
            for pat, binding in self.bind.items():
                if isinstance(binding, Use) and binding.store is not None:
                    problems.add(
                        "bind", f"{pat!r} has store={binding.store!r} but the machine has no store"
                    )
            if self.knobs["snapshot_every"]:
                problems.add("knobs", "snapshot_every needs a machine store (store=)")

        # Programs into code memory
        if program is not None:
            programs = {**(programs or {}), program: [entry]}
        if not programs:
            problems.add("program", "no program given")
            programs = {}
        self.code = CodeMemory()
        self.default_entry = entry
        self.lanes: list[Lane] = []
        self._lane_of_unit: dict[int, Lane] = {}
        self._used: set[str] = set()
        for prog, entries in programs.items():
            entries = [entries] if isinstance(entries, (str, type(None))) else list(entries)
            staged = self._stage(prog, entries, problems)
            self._commit(staged)
        if self.default_entry is None and self.code.entries:
            self.default_entry = next(iter(self.code.entries))

        # Bindings not used by any node
        for pat in self.bind:
            if pat not in self._used:
                problems.warn("bind", f"pattern {pat!r} matches no node")
        problems.raise_if_any()
        self.warnings = problems.warnings

        # Units start, all at once, every failure listed; none start in check mode
        self.dispatcher = Dispatcher(self, seed)
        if start_units and not _NO_START.get():
            units = [u for lane in self.lanes for u in lane.units]
            failures = start_units_of(units, self.start_timeout_s, self._lane_of_unit)
            if failures:
                stop_all(units)
                raise ConstructionError(failures)
            self.units_started = True

        # Lanes wired to the dispatcher; plugins loaded
        for lane in self.lanes:
            self._wire(lane)
        self.plugins.load_all()
