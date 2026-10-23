"""Deadline arbitration and attribution shared by Python and native lanes.

Only enforcement creates policy_failure; a host exception named ItemTimeout does not.
"""

from togeari.core.contracts.results import ItemError, ItemTimeout


def current(item):
    """Follow pre-submit replacements so all timers observe one settlement identity."""
    path = []
    seen = set()
    while "_deadline_replacement" in item.ext:
        if id(item) in seen:
            raise RuntimeError("cyclic deadline replacement")
        seen.add(id(item))
        path.append(item)
        item = item.ext["_deadline_replacement"]
    for previous in path:
        previous.ext["_deadline_replacement"] = item
    return item


def replace(original, replacement):
    """Transfer timer authority without resetting cap/source or retaining stale forward links."""
    merged = {**original.ext, **replacement.ext}
    merged.pop("_deadline_replacement", None)
    for name in ("deadline_cap", "call_timeout_s", "policy_source", "retries", "ordered_once"):
        if name in original.ext:
            merged[name] = original.ext[name]
        else:
            merged.pop(name, None)
    replacement.ext = merged
    replacement.deadline = original.deadline
    original.ext["_deadline_replacement"] = replacement


def call_policy(item) -> dict:
    return {
        "reason": "deadline",
        "scope": "call",
        "timeout_s": item.ext.get("call_timeout_s"),
        "source": item.ext.get("policy_source", "work_item"),
    }


def arm(item, now: float, attempt_timeout: float | None) -> float | None:
    """Choose the earlier call/attempt deadline; ties preserve the call policy."""
    cap = item.ext.get("deadline_cap")
    deadline = cap
    policy = call_policy(item)
    if attempt_timeout is not None:
        attempt = now + attempt_timeout
        if deadline is None or attempt < deadline:
            deadline = attempt
            policy = {
                "reason": "deadline",
                "scope": "attempt",
                "timeout_s": attempt_timeout,
                "source": "lane",
            }
    item.ext.pop("deadline_policy", None)
    item.ext.pop("deadline", None)
    item.ext.pop("timeout_s", None)
    item.deadline = deadline
    if deadline is not None:
        item.ext["deadline_policy"] = policy
        item.ext["deadline"] = deadline
        item.ext["timeout_s"] = round(deadline - now, 6)
    return deadline


def expire(item, now: float, inflight: bool) -> ItemError | None:
    """At the dispatcher's observation time, reject an expired item exactly once."""
    if item.ext.get("settled") or item.ext.get("squashed"):
        return None
    deadline = item.ext.get("deadline" if inflight else "deadline_cap")
    if deadline is None or now < deadline:
        return None
    policy = item.ext.get("deadline_policy") if inflight else call_policy(item)
    item.ext["policy_failure"] = dict(policy or call_policy(item))
    message = (
        f"{item.node}: over {item.ext.get('timeout_s')}s"
        if inflight
        else f"{item.node}: past its deadline while queued"
    )
    return ItemError(ItemTimeout(message))
