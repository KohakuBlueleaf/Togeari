"""Stubs of ``togeari._rs``, the native core (``src/togeari-rust``)."""

from typing import TypedDict

class CoreInfo(TypedDict):
    version: str
    dispatcher: bool
    debug: bool

__version__: str
DISPATCHER: bool

def core_info() -> CoreInfo:
    """The extension: ``version``, ``dispatcher`` (the native dispatcher is built) and
    ``debug`` (a debug build)."""
