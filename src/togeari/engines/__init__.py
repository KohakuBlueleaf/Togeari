"""Builtin engines, written against the same contracts as any project engine (spec/04).

Each engine lives in its own subpackage so its optional dependency loads only when used:

- ``togeari.engines.python``: ``PythonCallEngine`` (no extra dependency);
- ``togeari.engines.web``: ``WebCallEngine`` (``httpx``, extra ``[web]``);
- ``togeari.engines.torch``: ``TorchEngine`` (``torch``, extra ``[torch]``).
"""
