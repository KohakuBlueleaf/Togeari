"""Builtin adapters; none of them imports an optional dependency."""

from togeari.adapters.http import HttpRequest, JsonHttp
from togeari.adapters.torch import TorchAdapter
from togeari.core.contracts.adapter import PythonAdapter, PythonCall

__all__ = ["HttpRequest", "JsonHttp", "PythonAdapter", "PythonCall", "TorchAdapter"]
