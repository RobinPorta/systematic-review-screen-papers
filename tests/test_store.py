"""The in-memory session store: survives reloads, bounded by idle time and count."""

from __future__ import annotations

from screener.ui.store import SessionStore


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_session_is_found_again_with_its_data():
    store = SessionStore()
    store.create("a")["records"] = [1, 2, 3]
    assert store.get("a") == {"records": [1, 2, 3]}


def test_an_unknown_session_is_none():
    assert SessionStore().get("nope") is None


def test_a_session_idle_for_longer_than_the_limit_is_dropped():
    clock = Clock()
    store = SessionStore(idle_seconds=60, clock=clock)
    store.create("a")
    clock.now = 59
    assert store.get("a") is not None
    clock.now = 59 + 61
    assert store.get("a") is None


def test_activity_keeps_a_session_alive():
    clock = Clock()
    store = SessionStore(idle_seconds=60, clock=clock)
    store.create("a")
    for minute in range(1, 10):
        clock.now = minute * 50
        assert store.get("a") is not None


def test_beyond_the_cap_the_longest_idle_session_is_dropped():
    clock = Clock()
    store = SessionStore(max_sessions=3, clock=clock)
    for tick, token in enumerate("abc"):
        clock.now = tick
        store.create(token)
    clock.now = 10
    store.get("a")          # "a" is the oldest but active again, so "b" is now longest idle
    store.create("d")
    assert len(store) == 3
    assert store.get("b") is None
    assert all(store.get(t) is not None for t in "acd")


def test_twenty_one_sessions_keep_twenty():
    store = SessionStore()
    for n in range(21):
        store.create(f"s{n}")
    assert len(store) == 20
    assert store.get("s0") is None


def test_discard_frees_a_session():
    store = SessionStore()
    store.create("a")
    store.discard("a")
    assert store.get("a") is None
