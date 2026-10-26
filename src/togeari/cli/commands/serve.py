"""``togeari serve machine.py``: keep a machine module's machine running as an HTTP service.

Jobs come in over ``POST /jobs`` (``togeari.serve``); the module's sink, when it gives one,
keeps every record, its source is not read. The module's ``prepare`` and ``present`` (or
``--prepare`` / ``--present`` as ``module:attr``) shape jobs and answers. ``--telemetry DIR``
adds an inspector and the telemetry plugin, so ``togeari dash DIR`` shows the live machine.
"""

import argparse
import os
import signal
import threading
from typing import TextIO

from togeari.cli.loading import build_module, resolve
from togeari.core.construct.errors import ConstructionError
from togeari.plugins import Inspector
from togeari.serve import Service, default_prepare, default_present

try:
    from togeari.plugins.telemetry import Telemetry
except ImportError as exc:
    Telemetry = None
    MISSING = exc


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("serve", help="keep a machine module's machine running as an HTTP service")
    p.add_argument("machine", help="a Python file defining build()")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--token-env", default=None, help="an environment variable holding the token")
    p.add_argument("--telemetry", default=None, help="telemetry directory (inspector + dash)")
    p.add_argument("--prepare", default=None, help="module:attr turning a posted job into inputs")
    p.add_argument("--present", default=None, help="module:attr turning a record into a line")


def run(args: argparse.Namespace, out: TextIO) -> int:
    if args.telemetry and Telemetry is None:
        print(f"--telemetry needs the telemetry extra: {MISSING}", file=out)
        return 2
    try:
        module, machine, _, sink = build_module(args.machine)
    except ConstructionError as exc:
        print(exc, file=out)
        return 1
    prepare = resolve(args.prepare) if args.prepare else getattr(module, "prepare", None)
    present = resolve(args.present) if args.present else getattr(module, "present", None)
    plugins = []
    if args.telemetry:
        plugins = [Inspector(), Telemetry(args.telemetry, inspector_token=True)]
    service = Service(
        machine,
        sink,
        prepare or default_prepare,
        present or default_present,
        args.host,
        args.port,
        os.environ.get(args.token_env) if args.token_env else None,
        plugins,
    )
    return serve_until_signal(service, out)


def serve_until_signal(service: Service, out: TextIO) -> int:
    """Start ``service``; on SIGINT / SIGTERM stop taking jobs and drain the ones taken."""
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(f"togeari serve on {service.start()}", file=out, flush=True)
    stop.wait()
    print("draining", file=out, flush=True)
    service.stop(drain=True)
    return 0
