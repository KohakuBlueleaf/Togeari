"""The CLI commands; each module has ``add(subparsers)`` and ``run(args, out) -> exit code``."""

from togeari.cli.commands import check, conform, decode, doctor, run, units

COMMANDS = {
    "check": check,
    "decode": decode,
    "run": run,
    "conform": conform,
    "units": units,
    "doctor": doctor,
}

__all__ = ["COMMANDS"]
