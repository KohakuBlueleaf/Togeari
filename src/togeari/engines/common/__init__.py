"""Pieces shared by the builtin engines: tickets, error types, the process transport."""

from togeari.engines.common.errors import EngineStopped, RemoteError, WorkerDied
from togeari.engines.common.ticket import Ticket, finish

__all__ = ["EngineStopped", "RemoteError", "Ticket", "WorkerDied", "finish"]
