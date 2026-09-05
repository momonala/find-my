"""The library database: every asset iCloud has shown us, and which of them this drive holds.

It lives on the drive because it is the durable record: an asset deleted from
iCloud keeps its row, and from then on this file is the only thing describing
it. `assets` is what iCloud reported, `files` is what is on this drive.

Rollback journalling rather than WAL, because the drive is exFAT and removable
and WAL would strand `-wal`/`-shm` files beside the media.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from icloud_photo_metadata.models import GpsFix
from icloud_photo_metadata.models import PhotoMetadata
from icloud_photo_metadata.models import StoredResource

LIBRARY_NAME = "library.db"
DUMP_NAME = LIBRARY_NAME + ".dump"
SCHEMA_VERSION = 1

# A Live Photo has both. The still leads, because it is the asset.
ORIGINAL = "original"
LIVE_VIDEO = "live_video"
_FILE_ORDER = (ORIGINAL, LIVE_VIDEO)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id                       TEXT PRIMARY KEY,
    master_id                TEXT NOT NULL,
    filename                 TEXT NOT NULL,
    source_album             TEXT NOT NULL,

    in_icloud                INTEGER NOT NULL DEFAULT 1,
    icloud_gone_at           TEXT,
    first_seen_at            TEXT NOT NULL,

    item_type                TEXT NOT NULL,
    is_live_photo            INTEGER NOT NULL,
    asset_subtype_v2         INTEGER,
    hdr_type                 INTEGER,
    burst_flags              INTEGER,
    data_class_type          INTEGER,
    icloud_bytes             INTEGER NOT NULL,

    asset_date               TEXT NOT NULL,
    added_date               TEXT NOT NULL,
    original_creation_date   TEXT,
    import_date              TEXT,
    master_modified          TEXT,
    asset_modified           TEXT,
    time_zone_name           TEXT,
    time_zone_offset_seconds INTEGER,

    duration_seconds         REAL,
    video_frame_rate         REAL,
    orientation              INTEGER,
    original_orientation     INTEGER,

    is_favorite              INTEGER NOT NULL,
    is_hidden                INTEGER NOT NULL,
    rating                   INTEGER,
    title                    TEXT,
    description              TEXT,
    view_count               INTEGER,
    play_count               INTEGER,
    share_count              INTEGER,
    imported_by_bundle_id    TEXT,
    imported_by_display_name TEXT,

    latitude                 REAL,
    longitude                REAL,
    altitude                 REAL,
    gps_speed                REAL,
    gps_timestamp            TEXT,

    keywords                 TEXT,
    people                   TEXT,
    exif                     TEXT,

    camera_make  TEXT    GENERATED ALWAYS AS (json_extract(exif, '$."{TIFF}".Make'))               VIRTUAL,
    camera_model TEXT    GENERATED ALWAYS AS (json_extract(exif, '$."{TIFF}".Model'))              VIRTUAL,
    lens_model   TEXT    GENERATED ALWAYS AS (json_extract(exif, '$."{Exif}".LensModel'))          VIRTUAL,
    iso          INTEGER GENERATED ALWAYS AS (json_extract(exif, '$."{Exif}".ISOSpeedRatings[0]')) VIRTUAL,
    f_number     REAL    GENERATED ALWAYS AS (json_extract(exif, '$."{Exif}".FNumber'))            VIRTUAL,
    focal_length REAL    GENERATED ALWAYS AS (json_extract(exif, '$."{Exif}".FocalLength'))        VIRTUAL
);

CREATE TABLE IF NOT EXISTS asset_resources (
    asset_id     TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    size_bytes   INTEGER,
    content_type TEXT,
    width        INTEGER,
    height       INTEGER,
    checksum     TEXT,
    PRIMARY KEY (asset_id, kind)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS files (
    asset_id      TEXT NOT NULL REFERENCES assets(id),
    kind          TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    size_bytes    INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    recorded_at   TEXT NOT NULL,
    recorded_as   TEXT NOT NULL,
    PRIMARY KEY (asset_id, kind)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS marks (
    asset_id  TEXT PRIMARY KEY REFERENCES assets(id),
    mark      TEXT NOT NULL CHECK (mark IN ('delete', 'keep')),
    marked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS album_counts (
    album          TEXT PRIMARY KEY,
    reported_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS idx_assets_date       ON assets (asset_date DESC);
CREATE INDEX IF NOT EXISTS idx_assets_album_date ON assets (source_album, asset_date DESC);
CREATE INDEX IF NOT EXISTS idx_assets_camera     ON assets (camera_model);
CREATE INDEX IF NOT EXISTS idx_files_sha256      ON files (sha256);
"""

# The insert, the update and the row-to-model rebuild all read this, so they
# cannot drift. `in_icloud`, `icloud_gone_at` and `first_seen_at` are absent:
# they are this library's own bookkeeping, not anything iCloud reports.
_ASSET_COLUMNS = (
    "id",
    "master_id",
    "filename",
    "source_album",
    "item_type",
    "is_live_photo",
    "asset_subtype_v2",
    "hdr_type",
    "burst_flags",
    "data_class_type",
    "icloud_bytes",
    "asset_date",
    "added_date",
    "original_creation_date",
    "import_date",
    "master_modified",
    "asset_modified",
    "time_zone_name",
    "time_zone_offset_seconds",
    "duration_seconds",
    "video_frame_rate",
    "orientation",
    "original_orientation",
    "is_favorite",
    "is_hidden",
    "rating",
    "title",
    "description",
    "view_count",
    "play_count",
    "share_count",
    "imported_by_bundle_id",
    "imported_by_display_name",
    "latitude",
    "longitude",
    "altitude",
    "gps_speed",
    "gps_timestamp",
    "keywords",
    "people",
    "exif",
)

# Columns whose shape differs from the model field behind them.
_TRANSLATED_COLUMNS = frozenset(
    {
        "icloud_bytes",
        "latitude",
        "longitude",
        "altitude",
        "gps_speed",
        "gps_timestamp",
        "keywords",
        "people",
        "exif",
    }
)


class SchemaMismatch(RuntimeError):
    """The library on the drive was written by a different version of this tool."""


@dataclass(frozen=True)
class BackedUpFile:
    """One file on the drive that an asset claims."""

    kind: str
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class BackedUpAsset:
    """Every file this drive holds for one asset. Half an asset is not backed up."""

    asset_id: str
    files: tuple[BackedUpFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    def paths(self, directory: Path) -> list[Path]:
        return [directory / item.relative_path for item in self.files]


def connect(directory: Path) -> sqlite3.Connection:
    """Open the drive's library, creating the schema if the file is new.

    Raises:
        SchemaMismatch: If the file was written by a version this one cannot read.
    """
    conn = sqlite3.connect(directory / LIBRARY_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")

    found = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if found not in (0, SCHEMA_VERSION):
        conn.close()
        raise SchemaMismatch(
            f"{directory / LIBRARY_NAME} is schema v{found}, this tool speaks v{SCHEMA_VERSION}."
        )
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn


@contextmanager
def library(directory: Path) -> Iterator[sqlite3.Connection]:
    """A `connect` that always closes, for the scope of one command."""
    conn = connect(directory)
    try:
        yield conn
    finally:
        conn.close()


def replace_assets(conn: sqlite3.Connection, photos: Sequence[PhotoMetadata]) -> int:
    """Record a crawl that read every album end to end.

    Anything it did not return has left iCloud. One transaction, so a failure
    leaves the library as it was.

    Returns:
        How many assets iCloud still holds.
    """
    survivors = {photo.id for photo in photos}
    with conn:
        _upsert(conn, photos)
        _mark_gone(conn, [asset_id for asset_id in _live_ids(conn) if asset_id not in survivors])
        _stamp_pull_time(conn)
    return _live_count(conn)


def merge_assets(
    conn: sqlite3.Connection, fresh: Sequence[PhotoMetadata], *, exhaustive_albums: Sequence[str]
) -> int:
    """Lay a partial crawl over what the library already holds.

    `exhaustive_albums` were read end to end, so an asset filed under one that
    the crawl did not return has left it. Silence about any other asset means
    only that the crawl stopped before reaching it.

    Returns:
        How many assets iCloud still holds.
    """
    survivors = {photo.id for photo in fresh}
    with conn:
        _upsert(conn, fresh)
        departed = [asset_id for asset_id in _live_ids(conn, exhaustive_albums) if asset_id not in survivors]
        _mark_gone(conn, departed)
        _stamp_pull_time(conn)
    return _live_count(conn)


def restore_assets(conn: sqlite3.Connection, photos: Sequence[PhotoMetadata]) -> int:
    """Add assets the library has never seen, filed as no longer in iCloud.

    Only a pull can say an asset is still in iCloud, so one arriving from a
    backup record is backup-only until a pull says otherwise. `icloud_gone_at`
    stays null: no pull watched these leave. Assets already held are untouched,
    a pull's view of them being fresher than any record's.

    Returns:
        How many assets were new.
    """
    known = {row["id"] for row in conn.execute("SELECT id FROM assets")}
    unknown = [photo for photo in photos if photo.id not in known]
    columns = ", ".join(_ASSET_COLUMNS)
    placeholders = ", ".join("?" * len(_ASSET_COLUMNS))
    with conn:
        conn.executemany(
            f"INSERT INTO assets ({columns}, in_icloud, first_seen_at) VALUES ({placeholders}, 0, ?)",
            [(*_to_row(photo), _stamp(datetime.now(UTC))) for photo in unknown],
        )
        _write_resources(conn, unknown)
    return len(unknown)


def load_assets(conn: sqlite3.Connection, *, in_icloud_only: bool = False) -> list[PhotoMetadata]:
    """Every asset the library knows about, newest capture first.

    Args:
        in_icloud_only: Restrict to assets iCloud still holds. The default
            includes the ones this drive is now the only copy of.
    """
    resources: dict[str, dict[str, StoredResource]] = {}
    for row in conn.execute("SELECT * FROM asset_resources"):
        resources.setdefault(row["asset_id"], {})[row["kind"]] = StoredResource(
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            width=row["width"],
            height=row["height"],
            checksum=row["checksum"],
        )

    where = "WHERE in_icloud = 1" if in_icloud_only else ""
    rows = conn.execute(f"SELECT * FROM assets {where} ORDER BY asset_date DESC, id DESC")
    return [_to_photo(row, resources.get(row["id"], {})) for row in rows]


def write_dump(conn: sqlite3.Connection, directory: Path) -> int:
    """Write the SQL that recreates this library, and return its size in bytes.

    The copy that needs no SQLite to read, for a record nothing else can
    rebuild once iCloud has let an asset go. Temp-then-rename, so a crash
    cannot leave a half-written dump looking whole.
    """
    path = directory / DUMP_NAME
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w") as out:
            out.writelines(f"{line}\n" for line in conn.iterdump())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return path.stat().st_size


def known_ids(conn: sqlite3.Connection) -> set[str]:
    """Every asset id the library has ever seen, freed ones included.

    An incremental walk checks each page against this, so it must span assets
    already gone from iCloud: re-seeing one is not news.
    """
    return {row["id"] for row in conn.execute("SELECT id FROM assets")}


def live_album_counts(conn: sqlite3.Connection) -> Counter[str]:
    """How many assets iCloud still holds in each album, as this library sees it."""
    rows = conn.execute(
        "SELECT source_album, count(*) AS n FROM assets WHERE in_icloud = 1 GROUP BY source_album"
    )
    return Counter({row["source_album"]: row["n"] for row in rows})


def save_album_counts(conn: sqlite3.Connection, counts: dict[str, int]) -> None:
    """Record what iCloud reported, for the next pull to compare against."""
    with conn:
        conn.execute("DELETE FROM album_counts")
        conn.executemany("INSERT INTO album_counts VALUES (?, ?)", sorted(counts.items()))


def load_album_counts(conn: sqlite3.Connection) -> dict[str, int] | None:
    """The baseline the last pull left, or None if there is none to trust yet."""
    rows = conn.execute("SELECT album, reported_count FROM album_counts").fetchall()
    return {row["album"]: row["reported_count"] for row in rows} if rows else None


def pulled_at(conn: sqlite3.Connection) -> datetime | None:
    """When the metadata was last refreshed, or None if it never has been."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'pulled_at'").fetchone()
    return datetime.fromisoformat(row["value"]) if row else None


def backed_up(conn: sqlite3.Connection) -> dict[str, BackedUpAsset]:
    """Every asset this drive holds files for, keyed by asset id.

    Loaded whole so indexing stays a pure function over plain values.
    """
    grouped: dict[str, list[BackedUpFile]] = {}
    for row in conn.execute("SELECT * FROM files"):
        grouped.setdefault(row["asset_id"], []).append(
            BackedUpFile(
                kind=row["kind"],
                relative_path=row["relative_path"],
                size_bytes=row["size_bytes"],
                sha256=row["sha256"],
            )
        )
    return {
        asset_id: BackedUpAsset(asset_id=asset_id, files=tuple(sorted(files, key=_file_rank)))
        for asset_id, files in grouped.items()
    }


def _file_rank(item: BackedUpFile) -> int:
    """Raises ValueError on a kind this version does not know, rather than misfiling it."""
    return _FILE_ORDER.index(item.kind)


def record_files(
    conn: sqlite3.Connection, asset_id: str, files: Sequence[BackedUpFile], *, recorded_as: str
) -> None:
    """Note that this drive now holds `files` for `asset_id`.

    One commit per asset, so a crash cannot lose bookkeeping for bytes that
    already landed.
    """
    now = _stamp(datetime.now(UTC))
    with conn:
        conn.executemany(
            """
            INSERT INTO files (asset_id, kind, relative_path, size_bytes, sha256, recorded_at, recorded_as)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, kind) DO UPDATE SET
                relative_path = excluded.relative_path, size_bytes = excluded.size_bytes,
                sha256 = excluded.sha256, recorded_at = excluded.recorded_at,
                recorded_as = excluded.recorded_as
            """,
            [
                (asset_id, item.kind, item.relative_path, item.size_bytes, item.sha256, now, recorded_as)
                for item in files
            ],
        )


def _upsert(conn: sqlite3.Connection, photos: Sequence[PhotoMetadata]) -> None:
    """Write `photos` over whatever the library holds for them.

    `first_seen_at` survives an update and `in_icloud` is restored: an asset
    comes back when it is recovered from Recently Deleted, or when an earlier
    pull never reached it.
    """
    columns = ", ".join(_ASSET_COLUMNS)
    placeholders = ", ".join("?" * len(_ASSET_COLUMNS))
    updates = ", ".join(f"{name} = excluded.{name}" for name in _ASSET_COLUMNS if name != "id")
    conn.executemany(
        f"""
        INSERT INTO assets ({columns}, in_icloud, icloud_gone_at, first_seen_at)
        VALUES ({placeholders}, 1, NULL, ?)
        ON CONFLICT(id) DO UPDATE SET {updates}, in_icloud = 1, icloud_gone_at = NULL
        """,
        [(*_to_row(photo), _stamp(datetime.now(UTC))) for photo in photos],
    )
    _write_resources(conn, photos)


def _write_resources(conn: sqlite3.Connection, photos: Sequence[PhotoMetadata]) -> None:
    conn.executemany("DELETE FROM asset_resources WHERE asset_id = ?", [(photo.id,) for photo in photos])
    conn.executemany(
        "INSERT INTO asset_resources VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (photo.id, kind, res.size_bytes, res.content_type, res.width, res.height, res.checksum)
            for photo in photos
            for kind, res in photo.resources.items()
        ],
    )


def _live_ids(conn: sqlite3.Connection, albums: Sequence[str] = ()) -> list[str]:
    """Ids the library still believes iCloud holds, across all albums or the named ones."""
    if not albums:
        return [row["id"] for row in conn.execute("SELECT id FROM assets WHERE in_icloud = 1")]
    placeholders = ",".join("?" * len(albums))
    rows = conn.execute(
        f"SELECT id FROM assets WHERE in_icloud = 1 AND source_album IN ({placeholders})", tuple(albums)
    )
    return [row["id"] for row in rows]


def _mark_gone(conn: sqlite3.Connection, asset_ids: Sequence[str]) -> None:
    """Record that assets have left iCloud, without forgetting them.

    `icloud_gone_at` is stamped only on the way out, so it says when an asset
    disappeared rather than when it was last looked for.
    """
    conn.executemany(
        "UPDATE assets SET in_icloud = 0, icloud_gone_at = ? WHERE id = ? AND in_icloud = 1",
        [(_stamp(datetime.now(UTC)), asset_id) for asset_id in asset_ids],
    )


def _live_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM assets WHERE in_icloud = 1").fetchone()[0])


def _stamp_pull_time(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('pulled_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_stamp(datetime.now(UTC)),),
    )


def _to_row(photo: PhotoMetadata) -> tuple[Any, ...]:
    """Flatten one asset into `_ASSET_COLUMNS` order."""
    fix = photo.location
    values: dict[str, Any] = {
        "icloud_bytes": photo.total_storage_bytes,
        "latitude": fix.latitude if fix else None,
        "longitude": fix.longitude if fix else None,
        "altitude": fix.altitude if fix else None,
        "gps_speed": fix.speed if fix else None,
        "gps_timestamp": _stamp(fix.timestamp) if fix and fix.timestamp else None,
        "keywords": json.dumps(photo.keywords) if photo.keywords is not None else None,
        "people": json.dumps(photo.people),
        "exif": json.dumps(photo.exif) if photo.exif is not None else None,
    }
    for name in _ASSET_COLUMNS:
        if name not in values:
            field = getattr(photo, name)
            values[name] = _stamp(field) if isinstance(field, datetime) else field
    return tuple(values[name] for name in _ASSET_COLUMNS)


def _to_photo(row: sqlite3.Row, resources: dict[str, StoredResource]) -> PhotoMetadata:
    """Rebuild the model a row was flattened from."""
    fields: dict[str, Any] = {name: row[name] for name in _ASSET_COLUMNS if name not in _TRANSLATED_COLUMNS}
    return PhotoMetadata.model_validate(
        fields
        | {
            "total_storage_bytes": row["icloud_bytes"],
            "keywords": json.loads(row["keywords"]) if row["keywords"] else None,
            "people": json.loads(row["people"]) if row["people"] else [],
            "exif": json.loads(row["exif"]) if row["exif"] else None,
            "location": _to_gps_fix(row),
            "resources": resources,
        }
    )


def _to_gps_fix(row: sqlite3.Row) -> GpsFix | None:
    if row["latitude"] is None:
        return None
    return GpsFix(
        latitude=row["latitude"],
        longitude=row["longitude"],
        altitude=row["altitude"],
        speed=row["gps_speed"],
        timestamp=row["gps_timestamp"],
    )


def _stamp(value: datetime) -> str:
    """Render a datetime as UTC ISO 8601, so string order is time order.

    Apple reports in UTC and carries the capture zone in
    `time_zone_offset_seconds`, so normalising here loses nothing.
    """
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()
