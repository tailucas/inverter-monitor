#!/usr/bin/env python
"""SingleFlight — coalesce concurrent calls so only one runs at a time.

Thread-safe: waiters block until the in-flight call completes and share
its result.  Used to serialise inverter queries (which cannot tolerate
concurrent socket access) between the timed polling loop and the Telegram
bot's on-demand /status command.
"""

import threading
from collections.abc import Callable
from typing import Any


class SingleFlight:
    """Coalesce concurrent calls: only one invocation of *fn* runs at once.

    If *call()* is entered while another thread is already inside *fn*,
    the second caller blocks and returns the same result (or re-raises
    the same exception) once *fn* completes.  After *fn* finishes, the
    next caller starts a fresh invocation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._in_flight = False
        self._result: Any = None
        self._exception: BaseException | None = None

    def call(self, fn: Callable[[], Any]) -> Any:
        """Execute *fn* or wait for an in-flight execution to finish.

        Args:
            fn: A zero-argument callable to execute.

        Returns:
            The return value of *fn* (shared by all concurrent callers).

        Raises:
            Any exception raised by *fn* is propagated to every caller.
        """
        with self._lock:
            if self._in_flight:
                # Another call is already running — wait for it.
                self._cond.wait()
                if self._exception is not None:
                    raise self._exception
                return self._result
            # No call in flight — we are the leader.
            self._in_flight = True
            self._exception = None

        try:
            result = fn()
        except BaseException as exc:
            with self._lock:
                self._in_flight = False
                self._exception = exc
                self._cond.notify_all()
            raise

        with self._lock:
            self._result = result
            self._in_flight = False
            self._cond.notify_all()
        return result
