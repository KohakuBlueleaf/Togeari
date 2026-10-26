"""The CLI commands; each module has ``add(subparsers)`` and ``run(args, out) -> exit code``."""

from togeari.cli.commands import check, conform, dash, decode, doctor, run, serve, units

COMMANDS = {
    "check": check,
    "decode": decode,
    "run": run,
    "serve": serve,
    "conform": conform,
    "units": units,
    "doctor": doctor,
    "dash": dash,
}

__all__ = ["COMMANDS"]
