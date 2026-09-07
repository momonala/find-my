"""Carrying out the gallery's marks, off the request thread.

The same work `python -m icloud_photo_metadata prune` does. Each asset costs a
CloudKit lookup and a write, and verifying one costs a hash off a USB disk, so a
few dozen marks already outlast a request.

The plan is built inside the worker rather than in the route that starts it, so
what gets deleted is what the marks said at the moment of deletion.
"""

from __future__ import annotations

from pathlib import Path

from icloud_photo_metadata import library
from icloud_photo_metadata import prune
from icloud_photo_metadata.icloud_source import authenticate
from src.web.photos.jobs import BackgroundJob


class Prune(BackgroundJob):
    """The one prune this process will run."""

    def __init__(self) -> None:
        super().__init__(
            name="photos-prune",
            opening_phase="Checking the drive holds what is marked",
            counters={"deleting": 0, "freed_icloud": 0, "freed_drive": 0, "blocked": 0, "failed": 0},
        )

    def _run(self, directory: Path, mark: str | None) -> None:
        with library.library(directory) as conn:
            plan = prune.build_plan(conn, directory, mark=mark)
            self._update(blocked=len(plan.blocked))
            if plan.is_empty:
                self._finish("done", _nothing_message(plan), blocked=len(plan.blocked))
                return

            waiting = len(plan.ready)
            self._update(phase=f"Deleting {waiting:,} asset{'s' if waiting != 1 else ''}", deleting=waiting)
            outcome = prune.apply_plan(plan, directory, conn, api=authenticate())

        self._finish(
            "done",
            _outcome_message(outcome, plan),
            freed_icloud=len(outcome.freed_icloud),
            freed_drive=len(outcome.freed_drive),
            failed=len(outcome.failures),
            blocked=len(plan.blocked),
        )


def _nothing_message(plan: prune.PrunePlan) -> str:
    if not plan.blocked:
        return "Nothing is marked."
    held = len(plan.blocked)
    return f"Nothing was deleted: {held:,} marked asset{'s' if held != 1 else ''} did not pass the check."


def _outcome_message(outcome: prune.PruneOutcome, plan: prune.PrunePlan) -> str:
    parts = [f"Deleted {len(outcome.freed_icloud):,} from iCloud."]
    if outcome.freed_drive:
        parts.append(f"Removed {len(outcome.freed_drive):,} from the drive.")
    if plan.blocked:
        parts.append(f"{len(plan.blocked):,} left marked: the drive's copy did not check out.")
    if outcome.failures:
        parts.append(f"{len(outcome.failures):,} failed; they are still marked.")
    return " ".join(parts)


pruning = Prune()
