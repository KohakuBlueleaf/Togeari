"""The ``budget`` plugin: caps on admitted jobs, calls per node and walltime.

- Past a cap, ``vote_admit`` stops admission while jobs are resident, and
  ``pre_admit`` rejects every further item (it reaches the sink as a
  ``Blocked`` failure), so a run with a capped source still drains.
- Past ``max_calls_per_node`` for a node, ``pre_submit`` blocks the item, which
  fails with ``ItemError(Blocked)``.
"""

import time

from togeari.core.contracts.io import SourceItem
from togeari.core.plugin.base import BasePlugin, PluginBlockError, PluginContext


class Budget(BasePlugin):
    name = "budget"
    priority = 20

    def __init__(
        self,
        max_jobs: int | None = None,
        max_calls_per_node: int | dict[str, int] | None = None,
        walltime_s: float | None = None,
    ):
        self.max_jobs = max_jobs
        self.max_calls_per_node = max_calls_per_node
        self.walltime_s = walltime_s
        self.jobs = 0
        self.calls: dict[str, int] = {}
        self.blocked = {"admit": 0, "submit": 0}
        self.t0: float | None = None

    def on_load(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    def on_machine_start(self) -> None:
        if self.t0 is None:
            self.t0 = time.monotonic()

    def contribute_queries(self) -> dict:
        return {"budget": lambda m: self.status()}

    # ------------------------------------------------------------ caps
    def cap_of(self, node: str) -> int | None:
        match self.max_calls_per_node:
            case dict() as caps:
                return caps.get(node)
            case _:
                return self.max_calls_per_node

    def exhausted(self) -> str | None:
        """Why admission is over, or None."""
        if self.max_jobs is not None and self.jobs >= self.max_jobs:
            return f"budget: {self.max_jobs} jobs admitted"
        if (
            self.walltime_s is not None
            and self.t0 is not None
            and time.monotonic() - self.t0 >= self.walltime_s
        ):
            return f"budget: walltime {self.walltime_s}s reached"
        return None

    def status(self) -> dict:
        return {
            "jobs": self.jobs,
            "max_jobs": self.max_jobs,
            "calls": dict(self.calls),
            "max_calls_per_node": self.max_calls_per_node,
            "elapsed_s": None if self.t0 is None else time.monotonic() - self.t0,
            "walltime_s": self.walltime_s,
            "blocked": dict(self.blocked),
            "exhausted": self.exhausted(),
        }

    # ------------------------------------------------------------ hooks
    def vote_admit(self, signals: dict) -> bool:
        return self.exhausted() is None

    def pre_admit(self, item: SourceItem) -> None:
        if self.t0 is None:
            self.t0 = time.monotonic()
        reason = self.exhausted()
        if reason is not None:
            self.blocked["admit"] += 1
            raise PluginBlockError(reason)
        return None

    def on_job_start(self, job: dict) -> None:
        self.jobs += 1

    def pre_submit(self, item, unit, lane) -> None:
        cap = self.cap_of(item.node)
        n = self.calls.get(item.node, 0)
        if cap is not None and n >= cap:
            self.blocked["submit"] += 1
            raise PluginBlockError(f"budget: node {item.node!r} reached {cap} calls")
        self.calls[item.node] = n + 1
        return None
