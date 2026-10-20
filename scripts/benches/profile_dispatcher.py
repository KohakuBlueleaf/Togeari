"""Profile of the dispatcher thread on the ceiling shapes (cProfile around its loop).

Writes, per shape, the functions by own time and by cumulative time, plus the run's
calls/s with the profiler on, to ``runs/p6/profile_<shape>.txt``.
"""

import cProfile
import io
import pstats
import sys
from pathlib import Path

import dispatcher_ceiling as ceiling

from togeari.core.runtime.dispatcher import Dispatcher

LOOP = Dispatcher.loop
PROFILES: list[cProfile.Profile] = []


def profiled_loop(self) -> None:
    """The dispatcher loop under a profiler kept for the report."""
    prof = cProfile.Profile()
    PROFILES.append(prof)
    prof.runcall(LOOP, self)


def report(name: str, row: dict, out: Path) -> None:
    buf = io.StringIO()
    buf.write(f"{name}: {row['calls_per_s']:.0f} calls/s with the profiler on\n\n")
    stats = pstats.Stats(*PROFILES, stream=buf)
    stats.sort_stats("tottime").print_stats(40)
    stats.sort_stats("cumulative").print_stats(40)
    out.write_text(buf.getvalue(), encoding="utf-8")


def main(folder: str) -> None:
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    Dispatcher.loop = profiled_loop

    # The ceiling bench's shapes, with the default knobs
    for name in ceiling.SHAPES:
        PROFILES.clear()
        row = ceiling.run_shape(name)
        report(name, row, out / f"profile_{name}.txt")
        print(f"{name}: {row['calls_per_s']:.0f} calls/s (profiled) -> {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "runs/p6")
