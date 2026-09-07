"""The long jobs the gallery starts and then polls, and the thread they run on.

Pulling from iCloud and deleting from it both run for minutes and both talk to
the same session, so neither can happen on a request thread and neither can
happen twice at once. What they have in common is all here: one worker, a state
dict a poll can read, and the two failures every job that touches iCloud shares.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC
from datetime import datetime
from typing import Any

import click

logger = logging.getLogger(__name__)

# `authenticate` prompts for a 2FA code on stdin, which a web request has no way
# to answer; it exits rather than hanging, and this says what to do about it.
NO_SESSION = (
    "Could not open an iCloud session. Run `uv run --group photos python -m "
    "icloud_photo_metadata plan` in a terminal to sign in, then try again."
)

_IDLE: dict[str, Any] = {
    "status": "idle",
    "phase": "",
    "message": "",
    "started_at": None,
    "finished_at": None,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class BackgroundJob:
    """One job this process will run at a time, and what it is doing.

    Every field a poll reads is written under the lock, so a status request
    never catches the worker mid-update.
    """

    def __init__(self, name: str, opening_phase: str, counters: dict[str, int]) -> None:
        """
        Args:
            name: The worker thread's name, for a stack trace to be readable.
            opening_phase: What `start` reports before the job says otherwise.
            counters: The job's own numbers, and what they read as before it runs.
        """
        self._name = name
        self._opening_phase = opening_phase
        self._counters = dict(counters)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {**_IDLE, **self._counters}

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(self, *args: Any) -> bool:
        """Kick the job off with `args`, unless it is already running.

        Returns:
            False if a run is already under way, which is a refusal rather than
            a failure: the run underway is doing what the caller asked for.
        """
        with self._lock:
            if self._state["status"] == "running":
                return False
            self._state = {
                **_IDLE,
                **self._counters,
                "status": "running",
                "phase": self._opening_phase,
                "started_at": _now(),
            }
        threading.Thread(target=self._guarded, args=args, name=self._name, daemon=True).start()
        return True

    def _run(self, *args: Any) -> None:
        raise NotImplementedError

    def _guarded(self, *args: Any) -> None:
        """Run the job, leaving no failure without a state a poll can read."""
        try:
            self._run(*args)
        except click.exceptions.Exit:
            # What `authenticate` raises: missing credentials, or a 2FA prompt
            # with no terminal to answer it on.
            self._finish("error", NO_SESSION)
        except Exception as error:  # the thread is the end of the line for it
            logger.exception("The %s job failed.", self._name)
            self._finish("error", f"{type(error).__name__}: {error}")

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _finish(self, status: str, message: str, **fields: Any) -> None:
        self._update(status=status, phase="", message=message, finished_at=_now(), **fields)
