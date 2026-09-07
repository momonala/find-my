"""Running an iCloud sync from the gallery, off the request thread.

The same work `python -m icloud_photo_metadata download` does, with its
progress on an object a poll can read instead of on a terminal. A pull plus a
few hundred downloads runs for minutes, so the request that starts it returns
immediately and the page asks how it is going.

One at a time, process-wide: two syncs would fight over the same session and
the same rows, and the second would find nothing left to do anyway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from icloud_photo_metadata import library
from icloud_photo_metadata.backup import MetadataPullFailed
from icloud_photo_metadata.backup import apply_plan
from icloud_photo_metadata.backup import survey
from src.web.photos.jobs import BackgroundJob


class Refresh(BackgroundJob):
    """The one sync this process will run."""

    def __init__(self) -> None:
        super().__init__(
            name="photos-refresh",
            # No opening phase: the spinning refresh button already says a
            # pull is under way, and the status line is for its progress.
            opening_phase="",
            counters={"downloading": 0, "downloaded": 0, "failed": 0},
        )

    def _run(self, directory: Path) -> None:
        try:
            with library.library(directory) as conn:
                found = survey(conn, directory, quiet=True)
                if found.plan.is_empty:
                    self._finish("done", "The drive already matches iCloud.")
                    return

                waiting = len(found.plan.downloads)
                self._update(
                    phase=f"Downloading {waiting:,} new file{'s' if waiting != 1 else ''}",
                    downloading=waiting,
                )
                outcome = apply_plan(found.plan, directory, conn, api=found.api)
        except MetadataPullFailed as incomplete:
            self._finish(
                "error",
                f"{incomplete.result.failed_pages} metadata pages failed; the library was left "
                "unchanged and nothing was downloaded.",
            )
        else:
            self._finish(
                "done",
                _outcome_message(outcome),
                downloaded=outcome.downloaded,
                failed=outcome.failed,
            )


def _outcome_message(outcome: Any) -> str:
    parts = [f"Downloaded {outcome.downloaded:,}."]
    if outcome.failed:
        parts.append(f"{outcome.failed:,} failed; run the refresh again to retry them.")
    if outcome.aborted:
        parts.append("The session kept expiring, so the rest was left for another run.")
    return " ".join(parts)


refresh = Refresh()
