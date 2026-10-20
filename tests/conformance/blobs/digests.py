"""Registers the digest of ``Blob`` (the way an engine package registers its tensor digest)."""

from blobs.kinds import Blob
from togeari.core.contracts.digest import register_digest


def blob_digest(value: Blob) -> bytes:
    return b"blob:" + value.data


register_digest(Blob, blob_digest)
