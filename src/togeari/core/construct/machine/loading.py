"""Loading programs: stage (decode, bind, check), then commit (make visible), at construction
or while running (the `load` command)."""

import logging
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from togeari.core.construct.errors import ConstructionError, Problems
from togeari.core.construct.units import stop_all
from togeari.core.lanes.lane import Lane
from togeari.core.program.bind import bind_region
from togeari.core.program.code import Region
from togeari.core.program.execution import current_execution, signature_hash, validate_bindings
from togeari.core.program.job import build_job_programs

log = logging.getLogger(__name__)


@dataclass
class Staged:
    """A load in progress: regions decoded and bound, lanes found, nothing visible yet.

    ``after``: a load in progress that stages one of the same programs; this load is
    staged again once that one ends.
    """

    entries: list[tuple[str, Region]] = field(default_factory=list)
    lanes: list[Lane] = field(default_factory=list)
    after: Future | None = None


class LoadStage:
    """Staging, committing and loading programs (mixed into ``Machine``)."""

    # ------------------------------------------------------------ staging
    def _stage(self, prog: Any, entries: list, problems: Problems) -> Staged:
        """Decode and bind a program's entries; collect every problem; register nothing."""
        validate_bindings(self)
        staged = Staged()
        jobs = build_job_programs(
            prog, entries, self.libraries, self.sig_path, self.passes, problems
        )
        for jp in jobs:
            pending = self._loading.get(jp.semantic_hash)
            if pending is not None:
                staged.after = pending
                return staged

        for jp in jobs:
            region = self.code.cached(jp, self._binding_fingerprint)
            if region is not None and region.execution is not None:
                nodes = [ins.node for ins in self.code.calls(region)]
                unknown_signature = any(
                    issue["path"].startswith("signatures.")
                    for issue in region.execution["completeness"]["unknown"]
                )
                same_functions = all(
                    ins.binding is not None and ins.binding.fn is jp.env.impl(ins.node)
                    for ins in self.code.calls(region)
                )
                if (
                    unknown_signature
                    or not same_functions
                    or signature_hash(jp, nodes)
                    != region.execution["identity"]["program"]["signature_hash"]
                ):
                    region = None
            if region is None:
                region = self.code.stage(jp)
                for e in region.problems:
                    problems.add(jp.source, e)
                bind_region(
                    self.code,
                    region,
                    self.bind,
                    lambda t: self.lane_of(t, staged, problems),
                    problems,
                    self._used,
                    self.defaults,
                    event_output=self.event_output,
                )
                region.binding_fingerprint = self._binding_fingerprint
                if not problems.items:
                    region.execution = current_execution(region, self.code, self)

            # Entry name: a clash with a committed entry takes the source's stem
            name = jp.entry or Path(jp.source).stem
            taken = {n for n, _ in staged.entries}
            known = self.code.entries.get(name)
            if (known is not None and known.region is not region) or name in taken:
                name = f"{Path(jp.source).stem}.{name}"
            if name in self._loading_names:
                problems.add("load", f"entry {name!r} is staged by another load in progress")
            staged.entries.append((name, region))

        self._check_lanes(staged.lanes, problems)
        return staged

    def _commit(self, staged: Staged) -> list[str]:
        """Make a staged load visible: regions, lanes, entries."""
        for _, region in staged.entries:
            self.code.commit(region)
        for lane in staged.lanes:
            self.lanes.append(lane)
            for u in lane.units:
                self._lane_of_unit.setdefault(id(u), lane)
            if self.dispatcher is not None:
                self._wire(lane)
        for name, region in staged.entries:
            self.code.add_entry(name, region)
        return [name for name, _ in staged.entries]

    def lane_of(self, target: Any, staged: Staged, problems: Problems) -> Lane:
        """A lane, or a one-unit lane for an engine given directly."""
        if isinstance(target, Lane):
            lane = target
        else:
            lane = self._lane_of_unit.get(id(target))
            if lane is None:
                lane = next((ln for ln in staged.lanes if target in ln.units), None)
            if lane is None:
                lane = Lane([target], name=getattr(target, "name", None))
                if lane.kind == "batch":
                    self._set_policy(
                        lane, "former", self.contributed_policies.get("default_former"), problems
                    )
        if lane not in self.lanes and lane not in staged.lanes:
            self._resolve_policies(lane, problems)
            staged.lanes.append(lane)
        return lane

    def _resolve_policies(self, lane: Lane, problems: Problems) -> None:
        """A lane's former and selector may name a contributed policy."""
        for attr in ("former", "select"):
            value = getattr(lane, attr, None)
            if not isinstance(value, str):
                continue
            if value in self.contributed_policies:
                self._set_policy(lane, attr, self.contributed_policies[value], problems)
            else:
                problems.add(f"lane {lane.name!r}", f"{attr}: no contributed policy {value!r}")

    @staticmethod
    def _set_policy(lane: Lane, attr: str, policy: Any, problems: Problems) -> None:
        """Set a lane policy; one the lane refuses (a missing contract method) is a problem."""
        try:
            setattr(lane, attr, policy)
        except (TypeError, ValueError) as exc:
            problems.add(f"lane {lane.name!r}", str(exc))

    def _check_lanes(self, lanes: list[Lane], problems: Problems) -> None:
        """New lanes: one contract kind, a former exactly on batch lanes, unit names unique."""
        named = {u.name: u for lane in self.lanes for u in lane.units}
        for lane in lanes:
            if lane.kind == "mixed":
                problems.add(f"lane {lane.name!r}", "units of different contract kinds")
            if lane.kind == "batch" and lane.former is None:
                problems.add(f"lane {lane.name!r}", "a batch lane needs a former")
            if lane.kind != "batch" and lane.former is not None:
                problems.add(f"lane {lane.name!r}", f"a {lane.kind} lane takes no former")
            if lane.max_inflight is not None and lane.max_inflight <= 0:
                problems.add(f"lane {lane.name!r}", "max_inflight must be positive")
            for u in lane.units:
                other = named.setdefault(u.name, u)
                if other is not u:
                    problems.add(f"lane {lane.name!r}", f"unit name {u.name!r} is used twice")

    # ------------------------------------------------------------ loading while running
    def load_now(self, program: Any, entries: list) -> Future:
        """Load another program while running (dispatcher thread, the `load` command).

        Nothing is visible until every check passes and every new unit has
        started (off the dispatcher thread); the Future holds the entry names. A
        program another load in progress stages is loaded after that load ends.
        """
        problems = Problems()
        staged = self._stage(program, list(entries), problems)
        if staged.after is not None:
            return self._load_after(staged.after, program, entries)
        if problems.items:
            self._discard(staged)
            problems.raise_if_any()

        fut: Future = Future()
        units = [u for lane in staged.lanes for u in lane.units]
        if not units or not self.units_started:
            fut.set_result(self._commit_loaded(staged, problems))
            return fut

        # Staged names and programs are taken until the units have started
        self._hold(staged, fut)

        def started(failures: list) -> None:
            self._release(fut)
            if failures:
                stop_all(units)
                self._discard(staged)
                fut.set_exception(ConstructionError(failures))
            else:
                fut.set_result(self._commit_loaded(staged, problems))

        self._start_off_thread(units, started, fut)
        return fut

    def _commit_loaded(self, staged: Staged, problems: Problems) -> list[str]:
        """Publish successful dynamic-load warnings once, never pending or failed staging."""
        names = self._commit(staged)
        self.warnings.extend(problems.warnings)
        for warning in problems.warnings:
            log.warning("%s", warning)
        return names

    def _discard(self, staged: Staged) -> None:
        """Drop a failed load's new regions from code memory, newest first."""
        for _, region in reversed(staged.entries):
            self.code.discard(region)

    def _hold(self, staged: Staged, fut: Future) -> None:
        for name, region in staged.entries:
            self._loading_names[name] = fut
            if self.code.cached(region.job) is None:
                self._loading[region.semantic_hash] = fut

    def _release(self, fut: Future) -> None:
        for table in (self._loading, self._loading_names):
            for k in [k for k, f in table.items() if f is fut]:
                del table[k]

    def _load_after(self, pending: Future, program: Any, entries: list) -> Future:
        """A Future for loading ``program`` again once ``pending`` (another load) ends."""
        fut: Future = Future()

        def again() -> None:
            try:
                inner = self.load_now(program, entries)
            except Exception as exc:  # noqa: BLE001
                fut.set_exception(exc)
                return
            inner.add_done_callback(lambda f: _copy(f, fut))

        inbox = self.dispatcher.inbox
        pending.add_done_callback(lambda _: inbox.put(("call", again, None)))
        return fut


def _copy(src: Future, dst: Future) -> None:
    exc = src.exception()
    if exc is not None:
        dst.set_exception(exc)
    else:
        dst.set_result(src.result())
