"""``togeari run machine.py [--limit N] [--report DIR]``: run a machine module to the end."""

import argparse
import json
from typing import TextIO

from togeari.cli.loading import build
from togeari.core.construct.errors import ConstructionError
from togeari.plugins.report.run_report import RunReport


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("run", help="run a machine module over its source into its sink")
    p.add_argument("machine", help="a Python file defining build()")
    p.add_argument("--limit", type=int, default=None, help="admit at most N source items")
    p.add_argument("--report", default=None, help="write a run report into DIR")


def run(args: argparse.Namespace, out: TextIO) -> int:
    try:
        machine, source, sink = build(args.machine)
    except ConstructionError as exc:
        print(exc, file=out)
        return 1
    if source is None or sink is None:
        machine.stop()
        print("the machine module gives no source or no sink", file=out)
        return 1

    # The run report joins the machine's plugins before the run starts; the machine stops either way
    try:
        if args.report:
            machine.command("plugin_add", plugin=RunReport(args.report)).result()
        summary = machine.run(source, sink, limit=args.limit)
    finally:
        machine.stop()
    print(json.dumps(summary, indent=1, default=repr), file=out)
    return 0
