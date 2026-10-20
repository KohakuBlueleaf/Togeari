"""The three P6 studies; each module has ``TITLE``, ``plan(scale)``, ``run(cfg, scale, root)``
and ``summarize(results, root)``."""

from p6.studies import a_events, b_knobs, c_batches

STUDIES = {"A": a_events, "B": b_knobs, "C": c_batches}
