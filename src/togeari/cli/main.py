"""``togeari``: check, decode, run, conform, units, doctor, dash (spec/06 §6.3)."""

import argparse
import sys
from typing import TextIO

from togeari import __version__
from togeari.cli.commands import COMMANDS


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="togeari", description="Togeari dataflow machine tools")
    p.add_argument("--version", action="version", version=f"togeari {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    for module in COMMANDS.values():
        module.add(sub)
    return p


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    args = parser().parse_args(argv)
    return COMMANDS[args.command].run(args, out or sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
