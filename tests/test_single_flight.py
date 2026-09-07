#!/usr/bin/env python
"""Unit tests for the SingleFlight concurrency primitive."""

import threading
import time
from typing import Any

import pytest

from app.single_flight import SingleFlight


def test_call_returns_result() -> None:
    """A normal call returns the function's return value."""
    sf = SingleFlight()
    result = sf.call(lambda: 42)
    assert result == 42


def test_call_returns_none() -> None:
    """A call whose fn returns None returns None."""
    sf = SingleFlight()
    result = sf.call(lambda: None)
    assert result is None


def test_concurrent_callers_coalesce() -> None:
    """Two concurrent callees: fn runs once; both get the same result."""
    sf = SingleFlight()
    call_count = 0
    entered = threading.Event()
    proceed = threading.Event()

    def slow_fn() -> str:
        nonlocal call_count
        call_count += 1
        entered.set()  # signal that fn is running
        proceed.wait(timeout=5)  # stay inside fn until released
        return "shared"

    results: list[Any] = []
    excs: list[BaseException] = []

    def caller() -> None:
        try:
            r = sf.call(slow_fn)
            results.append(r)
        except BaseException as e:
            excs.append(e)

    t1 = threading.Thread(target=caller)
    t1.start()
    entered.wait(timeout=5)  # wait until t1 is inside slow_fn

    t2 = threading.Thread(target=caller)
    t2.start()
    # tiny sleep so t2 reaches _cond.wait() before we release
    time.sleep(0.05)

    proceed.set()  # release slow_fn
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert call_count == 1
    assert len(results) == 2
    assert results[0] == "shared"
    assert results[1] == "shared"


def test_subsequent_call_invokes_fn_again() -> None:
    """After a completed call, the next caller starts a fresh invocation."""
    sf = SingleFlight()
    call_count = 0

    def fn() -> int:
        nonlocal call_count
        call_count += 1
        return call_count

    assert sf.call(fn) == 1
    assert sf.call(fn) == 2
    assert call_count == 2


def test_exception_propagated_to_all_waiters() -> None:
    """When fn raises, every waiter gets the same exception."""
    sf = SingleFlight()
    entered = threading.Event()
    proceed = threading.Event()

    def failing_fn() -> None:
        entered.set()
        proceed.wait(timeout=5)
        raise ValueError("boom")

    results: list[Any] = []
    excs: list[BaseException] = []

    def caller() -> None:
        try:
            r = sf.call(failing_fn)
            results.append(r)
        except ValueError as e:
            excs.append(e)

    t1 = threading.Thread(target=caller)
    t1.start()
    entered.wait(timeout=5)

    t2 = threading.Thread(target=caller)
    t2.start()
    time.sleep(0.05)

    proceed.set()  # release fn → exception raised
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(excs) == 2
    assert all(str(e) == "boom" for e in excs)


def test_after_exception_next_call_succeeds() -> None:
    """After a failing call, the next call runs fn again and returns normally."""
    sf = SingleFlight()
    call_count = 0

    def fn() -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first fail")
        return call_count

    with pytest.raises(RuntimeError, match="first fail"):
        sf.call(fn)

    result = sf.call(fn)
    assert result == 2
    assert call_count == 2


def test_waiter_during_exception_not_hung() -> None:
    """A waiter arriving while the exception handler runs is not stuck.

    The waiter either sees the exception (if it arrived before
    notify_all) or runs a fresh call (if it arrived after cleanup).
    Either is valid; the important thing is no thread hangs.
    """
    sf = SingleFlight()
    entered = threading.Event()
    proceed = threading.Event()

    def failing_fn() -> None:
        entered.set()
        proceed.wait(timeout=5)
        raise RuntimeError("boom")

    primary_excs: list[BaseException] = []

    def primary() -> None:
        try:
            sf.call(failing_fn)
        except RuntimeError as e:
            primary_excs.append(e)

    t1 = threading.Thread(target=primary)
    t1.start()
    entered.wait(timeout=5)

    # t2 arrives while t1 is handling the exception
    t2_results: list[Any] = []
    t2_excs: list[BaseException] = []

    def secondary() -> None:
        try:
            r = sf.call(lambda: "ok-after")
            t2_results.append(r)
        except RuntimeError as e:
            t2_excs.append(e)

    t2 = threading.Thread(target=secondary)
    t2.start()
    time.sleep(0.05)
    proceed.set()  # release fn → RuntimeError

    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(primary_excs) == 1
    # t2 either got the exception (arrived before notify_all)
    # or got "ok-after" (arrived after cleanup completes)
    assert (len(t2_results) == 1 and t2_results[0] == "ok-after") or (len(t2_excs) == 1)
