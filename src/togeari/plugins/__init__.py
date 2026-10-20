"""Builtin plugins: counters, run report, trace, conformance recorder, inspector, budget
(spec/10 §10.6).
"""

from togeari.plugins.limits.budget import Budget
from togeari.plugins.metrics.counters import Counters
from togeari.plugins.record.archive import Archive
from togeari.plugins.record.recorder import Recorder
from togeari.plugins.report.perfetto import Perfetto
from togeari.plugins.report.run_report import RunReport
from togeari.plugins.serve.inspector import Inspector

__all__ = ["Archive", "Budget", "Counters", "Inspector", "Perfetto", "Recorder", "RunReport"]
