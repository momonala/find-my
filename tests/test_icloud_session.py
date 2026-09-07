"""The session files are what stand between a restart and another 2FA prompt.

Two processes share them: the Find My poller and the photo tool. pyicloud
persists them with a truncating write and no lock, so what these pin is that a
read never lands in the middle of somebody else's write.
"""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from src.core import icloud_session

# Big enough that one write is many filesystem writes, which is what gives a
# concurrent reader a window to land in.
_PAYLOAD = {"session_token": "s" * 200_000, "trust_token": "t" * 200_000}

SessionFactory = Callable[[Path], Any]


def _session_class() -> type:
    """A throwaway stand-in for `PyiCloudSession`, built fresh per test.

    `harden` patches the class it is given, so a shared one would carry another
    test's wrappers. This is only the two methods the lock wraps, writing the
    way pyicloud does: truncate, then write.
    """

    class FakeSession:
        def __init__(self, directory: Path) -> None:
            self._cookie_directory = str(directory)

        @property
        def session_path(self) -> Path:
            return Path(self._cookie_directory) / "account.session"

        def _load_session_data(self) -> dict[str, Any]:
            try:
                return json.loads(self.session_path.read_text())
            except (OSError, json.JSONDecodeError):
                return {}

        def _save_session_data(self) -> None:
            with self.session_path.open("w") as handle:
                json.dump(_PAYLOAD, handle)

    return FakeSession


@pytest.fixture
def hardened(monkeypatch: pytest.MonkeyPatch) -> SessionFactory:
    """A session class with the lock applied, and a way to build one."""
    fake = _session_class()
    monkeypatch.setattr(icloud_session, "PyiCloudSession", fake)
    monkeypatch.setattr(icloud_session, "_patched", False)
    icloud_session.harden()
    return fake


def test_a_read_never_catches_a_write_half_done(hardened: SessionFactory, tmp_path: Path) -> None:
    writer = hardened(tmp_path)
    reader = hardened(tmp_path)
    writer._save_session_data()
    stop = threading.Event()

    def write_until_told() -> None:
        while not stop.is_set():
            writer._save_session_data()

    thread = threading.Thread(target=write_until_told)
    thread.start()
    try:
        seen = [reader._load_session_data() for _ in range(200)]
    finally:
        stop.set()
        thread.join()

    # Every read is the whole session, never part-way through one.
    assert all(row == _PAYLOAD for row in seen)


def test_the_lock_lives_beside_the_session_it_guards(hardened: SessionFactory, tmp_path: Path) -> None:
    hardened(tmp_path)._save_session_data()

    assert (tmp_path / icloud_session.LOCK_NAME).exists()


def test_a_session_with_nowhere_to_persist_still_works(hardened: SessionFactory, tmp_path: Path) -> None:
    """pyicloud allows an unset cookie directory; there is then nothing to lock."""
    session = hardened(tmp_path)
    session._cookie_directory = ""

    assert session._load_session_data() == {}
    assert not (tmp_path / icloud_session.LOCK_NAME).exists()


def test_hardening_twice_does_not_nest_the_wrappers(hardened: SessionFactory) -> None:
    """Every layer takes the lock again, and a second one would be for nothing."""
    once = hardened._save_session_data

    icloud_session.harden()

    assert hardened._save_session_data is once
