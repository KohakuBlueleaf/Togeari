"""``togeari conform DIR``: replay recorded jobs through the reference interpreter
(spec/07 §7.1).

Prints each failure (with the job's weak values and weak digests), one line per
run (jobs, conforming jobs, jobs with weak values, weak digests) and the total.
"""

import argparse
from typing import TextIO

from togeari.cli.replay import replay_dir
from togeari.conformance.replay import STEP_BUDGET


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("conform", help="replay the records of a run through kohakunode")
    p.add_argument("dir", help="a run report or recorder directory")
    p.add_argument("--lib", action="append", default=[], help="node libraries as module:attr")
    p.add_argument("--max-fail", type=int, default=20, help="print at most N failures")
    p.add_argument("--run", action="append", default=[], help="replay only these run ids")

    # What the replay re-executes, and how it counts engine failures
    p.add_argument(
        "--reexecute",
        action="append",
        default=[],
        metavar="NODE",
        help="run this node again and compare with its record (repeatable)",
    )
    p.add_argument(
        "--engine-errors",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="a recorded failure of a re-executed node that the node does not raise is an"
        " engine failure (default), or with --no-engine-errors a mismatch",
    )

    # The reference's per-job instruction bound
    p.add_argument(
        "--step-budget",
        type=int,
        default=STEP_BUDGET,
        help=f"instructions the reference may execute per job (default {STEP_BUDGET:_})",
    )


def _weak_text(weak: dict) -> str:
    return ", ".join(f"{t} x{n}" for t, n in sorted(weak.items())) or "none"


def run(args: argparse.Namespace, out: TextIO) -> int:
    total = passed = weak = digests = event_unverified = event_mismatch = 0
    failures: list[str] = []
    runs: dict[str, dict] = {}

    # Replay every recorded job
    replayed = replay_dir(
        args.dir,
        args.lib,
        args.run or None,
        args.reexecute or None,
        args.step_budget,
        args.engine_errors,
    )
    for r in replayed:
        n_weak = sum(r.weak_digests.values())
        event_unverified += r.verdict.event_status == "unverified"
        event_mismatch += r.verdict.event_status == "mismatch"
        per = runs.setdefault(r.run, {"jobs": 0, "ok": 0, "weak": 0, "digests": 0})
        total += 1
        per["jobs"] += 1
        weak += bool(r.weak)
        per["weak"] += bool(r.weak)
        digests += n_weak
        per["digests"] += n_weak
        if r.verdict.ok:
            passed += 1
            per["ok"] += 1
            continue
        failures.append(
            f"{r.key} ({r.entry}): {r.verdict.reason}\n"
            f"    reference {r.verdict.reference!r}\n    machine   {r.verdict.machine!r}\n"
            f"    weak values {len(r.weak)}, weak digests {_weak_text(r.weak_digests)}"
        )

    # Report
    for line in failures[: args.max_fail]:
        print(f"FAIL {line}", file=out)
    for name, per in runs.items():
        print(
            f"run {name}: {per['ok']}/{per['jobs']} conform; {per['weak']} with weak values;"
            f" {per['digests']} weak digests",
            file=out,
        )
    print(
        f"{passed}/{total} jobs conform; {weak} with weak values; {digests} weak digests",
        file=out,
    )
    if event_unverified or event_mismatch:
        print(
            f"semantic events: {event_unverified} unverified, {event_mismatch} mismatched; "
            "consumer delivery/acknowledgment is not replayed",
            file=out,
        )
    return 0 if total and passed == total else 1
