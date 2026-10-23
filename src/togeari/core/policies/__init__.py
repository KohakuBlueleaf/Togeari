"""Machine-side builtin policies: admission, issue order, batch forming, unit choice.

``Former`` and ``Selector`` are the contracts of ``togeari.core.contracts.policies``,
with their default methods, for policies written as subclasses.
"""

from togeari.core.contracts.policies import READY, Former
from togeari.core.policies.admission import All, Fixed, MemoryBound, ResourcePressure, TargetLoad
from togeari.core.policies.former import Bucketed, Greedy, SizeOrWait
from togeari.core.policies.issue import (
    DeepestFirst,
    Fifo,
    HintPriority,
    LaneAware,
    OldestJobFirst,
)
from togeari.core.policies.resources import BudgetFormer
from togeari.core.policies.select import (
    Affinity,
    ByHint,
    LeastLoaded,
    RoundRobin,
    Selector,
    Throughput,
)

__all__ = [
    "READY",
    "Affinity",
    "All",
    "Bucketed",
    "BudgetFormer",
    "ByHint",
    "DeepestFirst",
    "Fifo",
    "Fixed",
    "Former",
    "Greedy",
    "HintPriority",
    "LaneAware",
    "LeastLoaded",
    "MemoryBound",
    "OldestJobFirst",
    "ResourcePressure",
    "RoundRobin",
    "Selector",
    "SizeOrWait",
    "TargetLoad",
    "Throughput",
]
