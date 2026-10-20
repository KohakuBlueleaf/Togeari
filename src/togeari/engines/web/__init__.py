"""``WebCallEngine``: any HTTP service as a call engine (engines/04)."""

from togeari.adapters.http import HttpRequest, JsonHttp
from togeari.engines.web.classify import default_classify, retry_after
from togeari.engines.web.engine import WebCallEngine
from togeari.engines.web.limiter import TokenBucket

__all__ = [
    "HttpRequest",
    "JsonHttp",
    "TokenBucket",
    "WebCallEngine",
    "default_classify",
    "retry_after",
]
