"""Togeari: a dataflow machine running many KIR jobs at once, out of order, on pluggable engines."""

from togeari.core.construct.errors import ConstructionError
from togeari.core.construct.machine import Machine, no_unit_start
from togeari.core.contracts import (
    MISS,
    BatchEngine,
    CallEngine,
    Cancelled,
    Engine,
    EngineBase,
    ItemError,
    JobRecord,
    Load,
    Ok,
    SourceItem,
    StreamEngine,
    UnitError,
    WorkItem,
)
from togeari.core.lanes.lane import Lane, Retry
from togeari.core.plugin.base import BasePlugin, PluginBlockError, PluginContext
from togeari.core.program.bind import Inline, Use
from togeari.core.runtime.handles import DispatcherDied

__version__ = "0.1.0.dev0"

__all__ = [
    "MISS",
    "BasePlugin",
    "BatchEngine",
    "CallEngine",
    "Cancelled",
    "ConstructionError",
    "DispatcherDied",
    "Engine",
    "EngineBase",
    "Inline",
    "ItemError",
    "JobRecord",
    "Lane",
    "Load",
    "Machine",
    "Ok",
    "PluginBlockError",
    "PluginContext",
    "Retry",
    "SourceItem",
    "StreamEngine",
    "UnitError",
    "Use",
    "WorkItem",
    "no_unit_start",
]
