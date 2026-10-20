"""The local echo service over HTTP, objects pickled into the request body (``Codec`` without
shm).
"""

import pickle
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from s1.workloads.http_echo import EchoService
from togeari.engines.common.transport import Codec


class HttpLink:
    name = "http"

    def __init__(self, timeout_s: float = 120.0):
        self.codec = Codec(shm=False)
        self.service = EchoService()
        self.timeout_s = timeout_s
        self.client: httpx.Client | None = None

    def open(self) -> None:
        self.service.start()
        self.client = httpx.Client(base_url=self.service.url, timeout=self.timeout_s)

    def _body(self, obj: object) -> bytes:
        packet, _ = self.codec.encode(obj)
        return packet.data

    def oneway(self, obj: object) -> float:
        t0 = time.perf_counter()
        r = self.client.post("/sink", content=self._body(obj))
        r.raise_for_status()
        return r.json()["t"] - t0

    def echo(self, obj: object) -> float:
        t0 = time.perf_counter()
        r = self.client.post("/mirror", content=self._body(obj))
        r.raise_for_status()
        pickle.loads(r.content)
        return time.perf_counter() - t0

    def _send(self, obj: object) -> None:
        self.client.post("/sink", content=self._body(obj)).raise_for_status()

    def stream(self, objs: list, window: int) -> float:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(window, thread_name_prefix="s1-http") as pool:
            list(pool.map(self._send, objs))
        return time.perf_counter() - t0

    def peer_cpu(self) -> float | None:
        return self.client.get("/cpu").json()["cpu"]

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.service.close()
