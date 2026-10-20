"""``togeari check machine.py``: build the machine and print every problem.

The module's ``build()`` runs inside ``no_unit_start()``: the machine is
constructed and checked, and no unit starts, whatever ``build()`` takes.
"""

import argparse
from typing import TextIO

from togeari.cli.loading import build
from togeari.core.construct.errors import ConstructionError
from togeari.core.construct.machine import no_unit_start


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("check", help="build a machine module and print every problem")
    p.add_argument("machine", help="a Python file defining build()")


def run(args: argparse.Namespace, out: TextIO) -> int:
    try:
        with no_unit_start():
            machine, _, _ = build(args.machine)
    except ConstructionError as exc:
        print(exc, file=out)
        return 1

    # A machine that builds: its warnings, then it stops
    try:
        for w in machine.warnings:
            print(f"warning {w}", file=out)
        entries = ", ".join(machine.code.entries)
        lanes = ", ".join(f"{ln.name}[{len(ln.units)}]" for ln in machine.lanes)
        print(f"ok: entries {entries}; lanes {lanes}", file=out)
    finally:
        machine.stop()
    return 0
