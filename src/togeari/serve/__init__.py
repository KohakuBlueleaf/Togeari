"""A machine kept running as an HTTP service (``togeari serve``)."""

from togeari.serve.service import Service, default_prepare, default_present

__all__ = ["Service", "default_prepare", "default_present"]
