"""Session data kept in server memory, so a page reload does not lose a visitor's work.

Streamlit's own `session_state` belongs to one browser connection and is gone on reload.
This store instead lives for the whole server process and is keyed by a random code in the
page URL (`?ws=…`), so reloading, or reopening the link, finds the same data again.

Nothing here touches disk. The store is bounded instead, because server memory is the
scarce resource: a session is dropped after an hour without activity, and once more than
`max_sessions` exist the one idle the longest is dropped to make room.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable

MAX_SESSIONS = 20
IDLE_SECONDS = 60 * 60


class SessionStore:
    """token -> that session's data dict, least recently used first."""

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        idle_seconds: float = IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_sessions = max_sessions
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._lock = threading.Lock()
        #: token -> (last activity, data). Ordered by last activity, oldest first.
        self._sessions: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def get(self, token: str) -> dict | None:
        """The session's data, marking it active. None if it expired or was dropped."""
        with self._lock:
            self._expire()
            entry = self._sessions.get(token)
            if entry is None:
                return None
            self._sessions[token] = (self._clock(), entry[1])
            self._sessions.move_to_end(token)
            return entry[1]

    def create(self, token: str) -> dict:
        """A fresh, empty session. Drops the longest-idle ones beyond `max_sessions`."""
        with self._lock:
            self._expire()
            data: dict = {}
            self._sessions[token] = (self._clock(), data)
            self._sessions.move_to_end(token)
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
            return data

    def discard(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._sessions)

    def _expire(self) -> None:
        cutoff = self._clock() - self.idle_seconds
        while self._sessions:
            token, (seen, _) = next(iter(self._sessions.items()))
            if seen >= cutoff:
                break
            del self._sessions[token]
