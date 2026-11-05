"""The CLI commands; each module has ``add(subparsers)`` and ``run(args, out) -> exit code``."""

from togeari.cli.commands import balance, check, conform, dash, decode, doctor, run, serve, units

COMMANDS = {
    "balance": balance,
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
