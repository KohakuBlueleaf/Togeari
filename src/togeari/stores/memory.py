"""An in-memory store (spec/05 §5.3)."""

import threading
from typing import Any

from togeari.core.contracts.io import MISS, copy_value


class MemoryStore:
    """Copies in, copies out: a value handed out never aliases the stored one."""

    def __init__(self) -> None:
        self.data: dict[bytes, Any] = {}
        self.lock = threading.Lock()

    def get(self, key: bytes) -> Any:
        with self.lock:
            value = self.data.get(key, MISS)
        return value if value is MISS else copy_value(value)

    def put(self, key: bytes, outputs: Any) -> None:
        value = copy_value(outputs)
        with self.lock:
            self.data[key] = value

    def delete(self, key: bytes) -> None:
        with self.lock:
            self.data.pop(key, None)

    def __len__(self) -> int:
        with self.lock:
            return len(self.data)
