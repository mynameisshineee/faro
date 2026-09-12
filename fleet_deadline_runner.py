"""Server-owned deadline scheduling over the existing Journal authority."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import coordination as C

_LOG = logging.getLogger(__name__)


class FleetDeadlineRunner:
    """One authenticated observer per lane; deadlines never execute recovery.

    Startup evaluates the durable state before serving. Subsequent checks use
    the Journal's clock and transaction, and shutdown joins the worker before
    the owner disposes that Journal. A failed worker stays visibly unhealthy.
    """

    def __init__(self, journal: C.Journal, *, credentials: tuple[str, ...] = (),
                 stale_after_s: int = 300, interval_s: int = 5,
                 clock: Callable[[], float] = time.time):
        if type(stale_after_s) is not int or not 1 <= stale_after_s <= 31 * 86400:
            raise ValueError("stale_after_s fuera de rango")
        if type(interval_s) is not int or not 1 <= interval_s <= 60:
            raise ValueError("interval_s fuera de rango")
        if type(credentials) is not tuple or any(
                type(c) is not str or not c for c in credentials):
            raise ValueError("credentials debe ser una tupla de credenciales")
        self._journal = journal
        self._credentials = credentials
        self._stale_after_s = stale_after_s
        self._interval_s = interval_s
        self._clock = clock
        self._sessions: list[C.IssuedSession] = []
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._credentials)

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return True
        return (self._thread is not None and self._thread.is_alive()
                and not self._failed.is_set() and not self._stop.is_set())

    def _tick(self) -> None:
        if not self._sessions:
            self._sessions = [self._journal.open_session(c) for c in self._credentials]
        for i, session in enumerate(self._sessions):
            if self._clock() >= session.expires_at - 60:
                session = self._journal.refresh_session(session.token)
                self._sessions[i] = session
            self._journal.evaluate_runtime_deadlines(
                session.token, stale_after_s=self._stale_after_s)

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("el runner de deadlines no se puede reutilizar")
        if not self.enabled:
            return
        try:
            self._tick()
        except Exception:
            self._failed.set()
            raise
        self._thread = threading.Thread(
            target=self._run, name="fleet-deadlines", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._tick()
            except Exception:
                self._failed.set()
                # No exception text, session, credential or target is logged.
                _LOG.error("fleet deadline worker stopped after evaluation failure")
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()
