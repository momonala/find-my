"""Carry out the deletions the gallery's marks decide on.

A mark says where an asset's bytes should stop existing. `icloud` frees the
iCloud copy and leaves the drive's, which is the whole point of having a backup.
`drive` is its mirror: the drive's copy goes and iCloud keeps the photo.
`everywhere` takes both, and is the only thing in this tool that lets go of the
last copy of a photo.

Whichever copy survives is what the gate below checks. An `icloud` mark is
refused unless the drive holds every file the library recorded for that asset,
at the recorded size, hashing to the recorded digest -- the surviving copy is
the drive's, so the drive's bytes have to be real. A `drive` mark leans on
iCloud instead and is refused when the asset is no longer there, but nothing is
hashed: iCloud's copy is not ours to verify. `everywhere` is not gated at all,
because discarding the last copy is what was asked for.

Deleting from iCloud sets `isDeleted`, which is what the Photos app does. The
asset lands in Recently Deleted and Apple keeps it for 30 days, so an `icloud`
delete is recoverable for a month and an `everywhere` delete is recoverable for
a month minus the local file. A `drive` delete is not recoverable from Apple at
all -- it never touches iCloud -- but the photo it leaves behind is still there.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from icloud_photo_metadata import library
from icloud_photo_metadata import queries
from icloud_photo_metadata.drive import sha256
from icloud_photo_metadata.icloud_source import AssetNotFound
from icloud_photo_metadata.icloud_source import DeleteFailed
from icloud_photo_metadata.icloud_source import delete_asset
from icloud_photo_metadata.icloud_source import fetch_asset
from icloud_photo_metadata.library import BackedUpFile
from icloud_photo_metadata.queries import DRIVE
from icloud_photo_metadata.queries import EVERYWHERE


# Which copies each mark takes. Every deletion below asks one of these rather
# than naming a mark, so the third one did not need a branch of its own.
_TAKES_ICLOUD = (queries.ICLOUD, EVERYWHERE)
_TAKES_DRIVE = (DRIVE, EVERYWHERE)


@dataclass(frozen=True)
class Target:
    """One marked asset and everything deciding its fate depends on."""

    asset_id: str
    filename: str
    mark: str
    source_album: str
    in_icloud: bool
    icloud_bytes: int
    files: tuple[BackedUpFile, ...]

    @property
    def drive_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)


@dataclass(frozen=True)
class Blocked:
    """A marked asset this run will not touch, and why not."""

    target: Target
    reason: str


@dataclass(frozen=True)
class PrunePlan:
    """What a prune would delete, and what it refuses to."""

    ready: tuple[Target, ...] = ()
    blocked: tuple[Blocked, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.ready

    @property
    def icloud_bytes(self) -> int:
        """What iCloud gets back, from the marks that name it."""
        return sum(
            target.icloud_bytes for target in self.ready if target.in_icloud and target.mark in _TAKES_ICLOUD
        )

    @property
    def drive_bytes(self) -> int:
        """What the drive gets back, which the two marks that name it free."""
        return sum(target.drive_bytes for target in self.ready if target.mark in _TAKES_DRIVE)


@dataclass(frozen=True)
class PruneOutcome:
    """What a prune actually did."""

    freed_icloud: tuple[str, ...] = ()
    freed_drive: tuple[str, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()
    icloud_bytes: int = 0
    drive_bytes: int = 0


def build_plan(conn: sqlite3.Connection, directory: Path, *, mark: str | None = None) -> PrunePlan:
    """Sort every pending mark into what this run will delete and what it will not.

    Verifying an `icloud` target means hashing its files off the drive, which is
    the one slow step here and the one worth paying for: it is the difference
    between trusting the library's bookkeeping and trusting the bytes.

    Args:
        mark: Restrict to one of `queries.MARKS`. The default plans them all.
    """
    held = library.backed_up(conn)
    ready: list[Target] = []
    blocked: list[Blocked] = []
    for row in queries.pending_marks(conn, mark):
        asset = held.get(row["asset_id"])
        target = Target(
            asset_id=row["asset_id"],
            filename=row["filename"],
            mark=row["mark"],
            source_album=row["source_album"],
            in_icloud=bool(row["in_icloud"]),
            icloud_bytes=row["icloud_bytes"],
            files=asset.files if asset else (),
        )
        reason = _refusal(target, directory)
        if reason is None:
            ready.append(target)
        else:
            blocked.append(Blocked(target=target, reason=reason))
    return PrunePlan(ready=tuple(ready), blocked=tuple(blocked))


def _refusal(target: Target, directory: Path) -> str | None:
    """Why this target must not be deleted, or None if it may be."""
    if not target.in_icloud and not target.files:
        return "already gone from iCloud and not on the drive"
    if target.mark == EVERYWHERE:
        return None
    if target.mark == DRIVE:
        if not target.files:
            return "nothing on the drive to delete"
        if not target.in_icloud:
            return "already gone from iCloud: the drive holds the only copy"
        return None
    if not target.in_icloud:
        return "already gone from iCloud"
    if not target.files:
        return "not backed up: the drive holds no copy to fall back on"
    return _unverified(target.files, directory)


def _unverified(files: Sequence[BackedUpFile], directory: Path) -> str | None:
    """The first recorded file whose bytes are not on the drive as recorded."""
    for item in files:
        path = directory / item.relative_path
        if not path.exists():
            return f"{item.relative_path} is recorded but missing from the drive"
        if path.stat().st_size != item.size_bytes:
            return f"{item.relative_path} is {path.stat().st_size:,} bytes, recorded as {item.size_bytes:,}"
        if sha256(path) != item.sha256:
            return f"{item.relative_path} does not hash to what was recorded"
    return None


def apply_plan(plan: PrunePlan, directory: Path, conn: sqlite3.Connection, *, api: Any) -> PruneOutcome:
    """Delete what the plan is ready to delete, one asset at a time.

    iCloud goes first and the drive's copy only once that has succeeded, so a
    failure leaves the asset with at least the copy it started with. The mark is
    stamped executed only when everything it asked for has happened.
    """
    freed_icloud: list[str] = []
    freed_drive: list[str] = []
    failures: list[tuple[str, str]] = []
    icloud_bytes = 0
    drive_bytes = 0

    for target in plan.ready:
        try:
            if target.in_icloud and target.mark in _TAKES_ICLOUD:
                delete_asset(fetch_asset(api, target.asset_id, target.source_album))
                library.record_gone(conn, target.asset_id)
                freed_icloud.append(target.asset_id)
                icloud_bytes += target.icloud_bytes
            if target.mark in _TAKES_DRIVE and target.files:
                _remove_from_drive(conn, target, directory)
                freed_drive.append(target.asset_id)
                drive_bytes += target.drive_bytes
        except (AssetNotFound, DeleteFailed, OSError) as error:
            failures.append((target.asset_id, str(error)))
            continue
        queries.record_executed(conn, target.asset_id)

    return PruneOutcome(
        freed_icloud=tuple(freed_icloud),
        freed_drive=tuple(freed_drive),
        failures=tuple(failures),
        icloud_bytes=icloud_bytes,
        drive_bytes=drive_bytes,
    )


def _remove_from_drive(conn: sqlite3.Connection, target: Target, directory: Path) -> None:
    """Unlink the asset's files, then forget them.

    The files go first: a row pointing at a file that is gone is a lie `verify`
    would report, while a file with no row is what a re-index already handles.
    """
    for item in target.files:
        (directory / item.relative_path).unlink(missing_ok=True)
    library.forget_files(conn, target.asset_id)
