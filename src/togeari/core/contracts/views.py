"""Read-only views policies receive: a ready statement (``Issue``), the job pool (``Admission``)."""

from types import MappingProxyType
from typing import Any

_NO_HINT: MappingProxyType = MappingProxyType({})


class ReadyView:
    """A ready statement as an issue policy sees it.

    ``job`` (the job's id, rising with admission order), ``job_key``, ``entry``,
    ``node``, ``key`` (the order key within the job), ``hint`` (the statement's
    ``@hint`` values), ``lane`` (the lane's name; None inline) and ``lane_fill``
    (items waiting in the lane per unit up; None inline).
    """

    __slots__ = ("_inf",)

    def __init__(self, inf: Any):
        self._inf = inf

    @property
    def job(self) -> int:
        return self._inf.job.id

    @property
    def job_key(self) -> str:
        return self._inf.job.key

    @property
    def entry(self) -> str:
        return self._inf.job.entry.name

    @property
    def node(self) -> str:
        return self._inf.instr.node

    @property
    def key(self) -> tuple:
        return self._inf.key

    @property
    def hint(self) -> MappingProxyType:
        hint = self._inf.instr.hint
        return MappingProxyType(hint) if hint else _NO_HINT

    @property
    def lane(self) -> str | None:
        b = self._inf.instr.binding
        return None if b is None or b.inline else b.lane.name

    @property
    def lane_fill(self) -> float | None:
        b = self._inf.instr.binding
        if b is None or b.inline:
            return None
        return b.lane.queued() / max(1, len(b.lane.units_up()))

    def __repr__(self) -> str:
        return f"<ready {self.node} job={self.job} key={self.key}>"


class PoolView:
    """The job pool as an admission policy sees it: ``jobs`` resident, their ``bytes``."""

    __slots__ = ("jobs", "bytes")

    def __init__(self, jobs: int, bytes: int):
        self.jobs = jobs
        self.bytes = bytes

    def __repr__(self) -> str:
        return f"<pool jobs={self.jobs} bytes={self.bytes}>"
