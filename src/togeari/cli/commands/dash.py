"""``togeari dash runs/``: serve the dashboard over every telemetry directory under the roots.

Needs the ``telemetry`` extra (numpy, kohakuvault).
"""

import argparse
from pathlib import Path
from typing import TextIO

try:
    from togeari.dash.server import serve
except ImportError as exc:
    serve = None
    MISSING = exc


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("dash", help="serve the dashboard over telemetry directories")
    p.add_argument("roots", nargs="+", help="directories searched for runs (machine.json)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--token", default=None, help="the inspector's token for live runs")
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="another Host / Origin the page is reached by (name, or name:port); repeatable",
    )


def run(args: argparse.Namespace, out: TextIO) -> int:
    if serve is None:
        print(f"togeari dash needs the telemetry extra: {MISSING}", file=out)
        return 2
    roots = [Path(r) for r in args.roots]
    server = serve(roots, args.host, args.port, args.token, args.allow_host)
    print(f"togeari dash on http://{args.host}:{server.server_address[1]}", file=out, flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
