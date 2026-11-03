"""Cache em memória com expiração, usado nas respostas do /sales-insights."""

import time
from collections import OrderedDict
from collections.abc import Hashable
from threading import Lock
from typing import Any


class TTLCache:
    """Guarda até maxsize itens por ttl segundos; seguro entre threads."""

    def __init__(self, maxsize: int, ttl: float):
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        return self.ttl > 0 and self.maxsize > 0

    def get(self, key: Hashable) -> Any | None:
        if not self.enabled:
            return None
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at < time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)  # usado recentemente
            return value

    def set(self, key: Hashable, value: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)  # remove o mais antigo

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
