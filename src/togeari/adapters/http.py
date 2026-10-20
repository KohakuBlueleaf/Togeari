"""HTTP payloads and the ``JsonHttp`` adapter (protocol ``"http"``)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kohakunode import Signature


@dataclass
class HttpRequest:
    """One HTTP request, relative to the engine's base URL."""

    method: str
    path: str
    json: Any = None
    body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _same(value: Any) -> Any:
    return value


class JsonHttp:
    """JSON over HTTP: ``build(inputs)`` is the request body, ``parse(json)`` the node result.

    ``decode`` takes the HTTP response, raises for a non-2xx status, and returns
    ``parse(response.json())``.
    """

    protocols = {"http"}
    light = True

    def __init__(
        self,
        path: str,
        build: Callable[[dict], Any] = _same,
        parse: Callable[[Any], Any] = _same,
        method: str = "POST",
        headers: dict[str, str] | None = None,
    ):
        self.path = path
        self.build = build
        self.parse = parse
        self.method = method
        self.headers = dict(headers or {})

    def check(self, signature: Signature) -> list[str]:
        return []

    def prepare(self, inputs: dict, protocol: str) -> HttpRequest:
        return HttpRequest(self.method, self.path, json=self.build(inputs), headers=self.headers)

    def decode(self, raw: Any, protocol: str) -> Any:
        raw.raise_for_status()
        return self.parse(raw.json())
