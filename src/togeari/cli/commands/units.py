"""``togeari units machine.py``: start the machine's units and run the engine behaviour checks.

The machine module gives the sample items through ``unit_samples``: a dict, or a
function of the machine returning one, of unit name -> list of ``WorkItem``s or
``(node, inputs)`` pairs (built with the node's bound adapter). It may give
``unit_factories``: unit name -> zero-argument function returning a fresh engine;
without one the check runs against the machine's unit. ``unit_check_args`` (unit
name -> keyword arguments of ``check_engine``, such as ``failing_items``, a
zero-argument ``kill`` or ``tolerance``) is passed through. Units without samples
are listed as skipped.
"""

import argparse
from typing import Any, TextIO

from togeari.cli.loading import build_module
from togeari.core.construct.errors import ConstructionError
from togeari.core.construct.machine import Machine
from togeari.core.contracts.workitem import WorkItem
from togeari.core.program.decode.instr import Op
from togeari.testing.check import EngineReport, check_engine


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("units", help="run the engine behaviour checks on every unit")
    p.add_argument("machine", help="a Python file defining build()")
    p.add_argument("--unit", action="append", default=[], help="check only these units")
    p.add_argument("--rounds", type=int, default=None, help="submit rounds per unit")


def sample_item(machine: Machine, sample: Any) -> WorkItem:
    """A ``WorkItem`` as given, or built from ``(node, inputs)`` with the node's bound adapter."""
    match sample:
        case WorkItem():
            return sample
        case (str() as node, dict() as inputs):
            for ins in machine.code.mem:
                b = ins.binding if ins.op == Op.CALL else None
                if ins.node == node and b is not None and not b.inline:
                    return WorkItem(node, dict(inputs), b.adapter)
            raise KeyError(f"node {node!r} is not bound to a lane")
        case _:
            raise TypeError(f"a sample is a WorkItem or (node, inputs), not {sample!r}")


def run(args: argparse.Namespace, out: TextIO) -> int:
    try:
        module, machine, _, _ = build_module(args.machine)
    except ConstructionError as exc:
        print(exc, file=out)
        return 1
    factories = getattr(module, "unit_factories", None) or {}
    samples = getattr(module, "unit_samples", None) or {}
    if callable(samples):
        samples = samples(machine) or {}
    extra = getattr(module, "unit_check_args", None) or {}
    failed = checked = 0
    try:
        for lane in machine.lanes:
            for unit in lane.units:
                if args.unit and unit.name not in args.unit:
                    continue
                where = f"{lane.name}/{unit.name}"
                if not samples.get(unit.name):
                    print(f"skipped {where}: no samples", file=out)
                    continue

                # One unit's checks
                checked += 1
                items = [sample_item(machine, s) for s in samples[unit.name]]
                factory = factories.get(unit.name) or (lambda u=unit: u)
                rounds = {} if args.rounds is None else {"rounds": args.rounds}
                kwargs = {**rounds, **extra.get(unit.name, {})}
                if "failing_items" in kwargs:
                    kwargs["failing_items"] = [
                        sample_item(machine, s) for s in kwargs["failing_items"]
                    ]
                try:
                    report: EngineReport = check_engine(factory, items, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print(f"FAIL {where}: {type(exc).__name__}: {exc}", file=out)
                    continue
                failed += not report.ok
                print(f"{'ok  ' if report.ok else 'FAIL'} {where}", file=out)
                print(str(report), file=out)
    finally:
        machine.stop()
    print(f"{checked - failed}/{checked} units pass", file=out)
    return 1 if failed or not checked else 0
