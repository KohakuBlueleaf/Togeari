"""Error types the builtin engines report."""


class EngineStopped(RuntimeError):
    """The engine is not running, or stopped while the item waited."""


class WorkerDied(RuntimeError):
    """The worker process running the item exited."""


class RemoteError(Exception):
    """The formatted traceback of an exception raised in a worker process."""
