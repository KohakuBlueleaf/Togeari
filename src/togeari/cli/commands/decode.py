"""``togeari decode prog.kir --entry E``: print the decode tables of a program's job programs."""

import argparse
from pathlib import Path
from typing import TextIO

from togeari.cli.loading import resolve
from togeari.core.construct.errors import ConstructionError, Problems
from togeari.core.program.code import CodeMemory
from togeari.core.program.job import build_job_programs


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("decode", help="print the decode tables of a program")
    p.add_argument("program", help="a .kir file")
    p.add_argument("--entry", action="append", help="an entry @def (repeatable); none: top level")
    p.add_argument(
        "--passes", nargs="*", default=None, help="lowering passes (default: the machine's)"
    )
    p.add_argument("--lib", action="append", default=[], help="a node library as module:attr")
    p.add_argument("--sig-path", action="append", default=[], help="a signature folder")


def run(args: argparse.Namespace, out: TextIO) -> int:
    libraries = [resolve(ref) for ref in args.lib]
    problems = Problems()
    jobs = build_job_programs(
        Path(args.program),
        args.entry or [None],
        libraries,
        [Path(p) for p in args.sig_path] or None,
        args.passes,
        problems,
    )

    # Decode into a fresh code memory, one region per entry
    code = CodeMemory()
    for jp in jobs:
        errs: list[str] = []
        region = code.load(jp, errs)
        for e in errs:
            problems.add(jp.source, e)
        print(code.dump(region), file=out)
        print(file=out)
    try:
        problems.raise_if_any()
    except ConstructionError as exc:
        print(exc, file=out)
        return 1
    return 0
