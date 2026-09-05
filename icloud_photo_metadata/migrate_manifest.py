"""One-time move of the drive's JSON manifest into the library database.

Nothing can rebuild this record for an asset already freed from iCloud, so the
import reads the library back and compares every record against the JSON rather
than trusting the write.

The JSON's schema lives here rather than in a module of its own, so deleting
this file deletes the last trace of the format. Nothing else imports it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict

from icloud_photo_metadata import library
from icloud_photo_metadata.library import BackedUpFile
from icloud_photo_metadata.models import PhotoMetadata

MANIFEST_NAME = ".backup-manifest.json"


class LegacyRecord(BaseModel):
    """One asset's entry in the JSON manifest.

    `extra="allow"`: a field this reader does not know is not a reason to
    refuse an import that cannot be repeated.
    """

    model_config = ConfigDict(extra="allow")

    relative_path: str
    size_bytes: int
    sha256: str
    recorded_at: datetime
    recorded_as: str
    live_relative_path: str | None = None
    live_size_bytes: int | None = None
    live_sha256: str | None = None
    metadata: dict[str, Any]


class LegacyManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    assets: dict[str, LegacyRecord] = {}


@dataclass(frozen=True)
class ImportResult:
    """What the import moved, and anything that did not survive the trip."""

    records: int = 0
    files: int = 0
    restored: int = 0
    discrepancies: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies


def import_manifest(conn: sqlite3.Connection, directory: Path) -> ImportResult:
    """Move `<directory>/.backup-manifest.json` into the library on `conn`.

    Safe before or after a pull: unseen assets are restored as backup-only, and
    a later pull promotes back whatever iCloud still holds.

    Raises:
        FileNotFoundError: If the drive has no manifest to import.
    """
    path = directory / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(f"No manifest at {path}.")

    manifest = LegacyManifest.model_validate_json(path.read_text())
    restored = library.restore_assets(
        conn, [PhotoMetadata.model_validate(record.metadata) for record in manifest.assets.values()]
    )
    _write_files(conn, manifest)

    stored = library.backed_up(conn)
    discrepancies = [
        problem
        for asset_id, record in sorted(manifest.assets.items())
        if (problem := _compare(asset_id, record, stored.get(asset_id)))
    ]
    return ImportResult(
        records=len(manifest.assets),
        files=sum(len(asset.files) for asset in stored.values()),
        restored=restored,
        discrepancies=discrepancies,
    )


def _write_files(conn: sqlite3.Connection, manifest: LegacyManifest) -> None:
    """One transaction rather than one per asset: this is a bulk load, not a sync."""
    rows = [
        (
            asset_id,
            item.kind,
            item.relative_path,
            item.size_bytes,
            item.sha256,
            record.recorded_at.isoformat(),
            record.recorded_as,
        )
        for asset_id, record in manifest.assets.items()
        for item in _files_of(record)
    ]
    with conn:
        conn.executemany(
            """
            INSERT INTO files (asset_id, kind, relative_path, size_bytes, sha256, recorded_at, recorded_as)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, kind) DO NOTHING
            """,
            rows,
        )


def _files_of(record: LegacyRecord) -> list[BackedUpFile]:
    files = [BackedUpFile(library.ORIGINAL, record.relative_path, record.size_bytes, record.sha256)]
    if record.live_relative_path:
        files.append(
            BackedUpFile(
                library.LIVE_VIDEO,
                record.live_relative_path,
                record.live_size_bytes or 0,
                record.live_sha256 or "",
            )
        )
    return files


def _compare(asset_id: str, record: LegacyRecord, stored: library.BackedUpAsset | None) -> str | None:
    """How the library's copy of one record differs from the JSON, or None."""
    if stored is None:
        return f"{asset_id}: no files recorded"
    expected = _files_of(record)
    if list(stored.files) != expected:
        return f"{asset_id}: {expected} became {list(stored.files)}"
    return None
