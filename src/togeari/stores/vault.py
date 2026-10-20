"""A store in a KohakuVault database (spec/05 §5.3; extra ``[vault]``)."""

import pickle
import threading
from pathlib import Path
from typing import Any

from kohakuvault import KVault

from togeari.core.contracts.io import MISS


class VaultStore:
    """Pickled outputs by store key, kept across runs; every get unpickles a fresh copy.

    The caller that opens the store closes it (``close()``, or ``with VaultStore(...)``),
    after the machines using it have stopped; a machine never closes a store it was given.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.kv = KVault(str(self.path))
        self.kv.disable_auto_pack()
        self.lock = threading.Lock()

    def get(self, key: bytes) -> Any:
        with self.lock:
            data = self.kv.get(key)
        return MISS if data is None else pickle.loads(data)

    def put(self, key: bytes, outputs: Any) -> None:
        data = pickle.dumps(outputs)
        with self.lock:
            self.kv[key] = data

    def delete(self, key: bytes) -> None:
        with self.lock:
            if self.kv.get(key) is not None:
                del self.kv[key]

    def __len__(self) -> int:
        with self.lock:
            return sum(1 for _ in self.kv.keys(limit=1 << 62))

    def close(self) -> None:
        with self.lock:
            if self.kv is not None:
                self.kv.close()
                self.kv = None

    def __enter__(self) -> "VaultStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
