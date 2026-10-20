"""The default mapping from an HTTP outcome to a result class."""

import email.utils
import time

import httpx

from togeari.core.contracts.results import ItemError, UnitError


def retry_after(response: httpx.Response) -> float | None:
    """Seconds from a ``Retry-After`` header (delay or HTTP date); None when absent or
    unreadable.
    """
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def status_error(response: httpx.Response) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"HTTP {response.status_code} from {response.request.method} {response.request.url}",
        request=response.request,
        response=response,
    )


def default_classify(outcome: httpx.Response | BaseException) -> ItemError | UnitError | None:
    """``UnitError`` for an unreachable service, 429 and 503; ``ItemError`` for other failures.

    ``outcome`` is the response, or the exception raised while building or
    sending the request. None means the response goes on to ``item.finish``.
    """
    match outcome:
        case httpx.Response(status_code=429 | 503):
            return UnitError(status_error(outcome), retry_after(outcome))
        case httpx.Response(status_code=code) if code >= 400:
            return ItemError(status_error(outcome))
        case httpx.Response():
            return None
        case httpx.ConnectError() | httpx.ConnectTimeout():
            return UnitError(outcome)
        case _:
            return ItemError(outcome)
