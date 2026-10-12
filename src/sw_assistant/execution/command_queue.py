"""Queue epochs prevent actions queued before Stop from running afterwards."""
from __future__ import annotations

import queue
import threading
from typing import Any


class CommandQueue:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[int, tuple[str, Any]]] = queue.Queue()
        self._lock = threading.Lock()
        self._epoch = 0

    def put(self, item: tuple[str, Any]) -> None:
        with self._lock:
            self._queue.put((self._epoch, item))

    def get(self) -> tuple[int, tuple[str, Any]]:
        return self._queue.get()

    def invalidate(self) -> None:
        with self._lock:
            self._epoch += 1

    def current(self, epoch: int) -> bool:
        with self._lock:
            return epoch == self._epoch
