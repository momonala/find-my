"""Make pyicloud's session files safe for more than one process at a time.

`.icloud_session/` holds the session token, the trust token and the cookie jar.
Two things read and write it: the Find My poller inside `mycloud serve`, and the
photo tool, which borrows the same trusted session rather than prompting for 2FA
of its own.

pyicloud persists both files with a plain truncating `open(path, "w")` on every
request that carries new session headers. Nothing serialises that. So a poll and
a photo command overlapping leaves a reader looking at a half-written file, the
JSON fails to parse, and pyicloud logs "Session file does not exist" and starts
from scratch -- which is a 2FA prompt on a session that was perfectly good.

An advisory file lock around every read and every write is enough: a reader
either waits for the write to finish or gets in before it starts. The lock is
per-directory, not per-file, because the session JSON and the cookie jar are
written as one update and have to be read as one.

Not covered: a process killed mid-write still truncates its file. Fixing that
means replacing pyicloud's save rather than wrapping it, which is a much larger
piece of its internals to own.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pyicloud.session import PyiCloudSession

LOCK_NAME = ".session.lock"

# Patching module-level state twice would nest the wrappers, and every layer
# takes the lock again.
_patched = False


@contextmanager
def _holding(directory: str | None, *, exclusive: bool) -> Iterator[None]:
    """Hold the session directory's lock for the block.

    A session with nowhere to persist to has nothing to protect, so it just
    runs: pyicloud allows `cookie_directory` to be unset.
    """
    if not directory:
        yield
        return

    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    with (path / LOCK_NAME).open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def harden() -> None:
    """Serialise pyicloud's session persistence across processes. Idempotent.

    Call before building a `PyiCloudService`. It patches the class, so it covers
    every session this process opens rather than one instance.
    """
    global _patched
    if _patched:
        return

    load = PyiCloudSession._load_session_data
    save = PyiCloudSession._save_session_data

    def locked_load(self: PyiCloudSession, *args: Any, **kwargs: Any) -> Any:
        with _holding(self._cookie_directory, exclusive=False):
            return load(self, *args, **kwargs)

    def locked_save(self: PyiCloudSession, *args: Any, **kwargs: Any) -> Any:
        with _holding(self._cookie_directory, exclusive=True):
            return save(self, *args, **kwargs)

    PyiCloudSession._load_session_data = locked_load  # type: ignore[method-assign]
    PyiCloudSession._save_session_data = locked_save  # type: ignore[method-assign]
    _patched = True
