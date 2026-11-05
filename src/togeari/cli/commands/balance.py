"""``togeari balance``: one job endpoint in front of many ``togeari serve`` services.

Services join by reporting (``togeari serve --balancer URL``) or are named here (``--server
URL``, read by polling). Clients post to the balancer as to one service (``togeari.serve``).
"""

import argparse
import os
import signal
import threading
from typing import TextIO

from togeari.serve import Balancer


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("balance", help="one job endpoint in front of many services")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--token-env", default=None, help="an environment variable holding the token")
    p.add_argument("--server", action="append", default=[], help="a service URL to poll")
    p.add_argument("--lead", type=float, default=2.0, help="seconds of work queued per server")
    p.add_argument("--min-queue", type=int, default=32, help="jobs per server before its rate")
    p.add_argument("--hold", type=float, default=30.0, help="seconds of work held (0: no cap)")
    p.add_argument("--batch", type=int, default=64, help="jobs per send to a server at most")
    p.add_argument("--dead", type=float, default=5.0, help="seconds without a report: down")


def run(args: argparse.Namespace, out: TextIO) -> int:
    balancer = Balancer(
        args.host,
        args.port,
        os.environ.get(args.token_env) if args.token_env else None,
        args.server,
        lead_s=args.lead,
        min_queue=args.min_queue,
        hold_s=args.hold or None,
        batch=args.batch,
        dead_s=args.dead,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(f"togeari balance on {balancer.start()}", file=out, flush=True)
    stop.wait()
    print("draining", file=out, flush=True)
    balancer.stop(drain=True)
    return 0
