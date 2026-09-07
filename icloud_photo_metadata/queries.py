"""Reads over the library for the gallery: filter, sort, page, and facet.

Filters are pushed into SQL rather than applied in Python, so a page costs one
query over an index instead of hydrating 9,711 models to throw most away.

Nothing here writes except `set_mark`, which is the one thing in the library a
person authors rather than a pull.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from icloud_photo_metadata import library

# `files` is joined for the drive-relative path the image route serves, so an
# asset that has never been backed up still lists.
_RESOURCES = "LEFT JOIN asset_resources r ON r.asset_id = a.id AND r.kind = 'original'"
_FILES = "LEFT JOIN files f ON f.asset_id = a.id AND f.kind = 'original'"
# Pending marks only, so `mark` means the same thing everywhere: a decision
# still waiting. A carried-out one stays in the table as the record that it
# happened, but it must not outline a card or answer a `mark=` filter.
_MARKS = "LEFT JOIN marks m ON m.asset_id = a.id AND m.executed_at IS NULL"

_JOINS = f"FROM assets a {_RESOURCES} {_FILES} {_MARKS}"

# Deliberately no generated column: each one runs json_extract over an exif
# blob, and reading 34 MB of them off the drive costs a grid page a second.
# The camera and its settings are detail-panel data, fetched one asset at a time.
_GRID_ROW = f"""
SELECT a.id, a.filename, a.asset_date, a.source_album, a.item_type, a.in_icloud,
       a.is_favorite, a.is_live_photo, a.duration_seconds, a.latitude,
       r.width, r.height, r.size_bytes AS original_bytes,
       f.relative_path, m.mark
{_JOINS}
"""

_DETAIL_ROW = f"""
SELECT a.*, r.width, r.height, r.size_bytes AS original_bytes, f.relative_path, m.mark
{_JOINS}
"""

SORTS: dict[str, str] = {
    "newest": "a.asset_date DESC, a.id DESC",
    "oldest": "a.asset_date ASC, a.id ASC",
    "largest": "r.size_bytes DESC NULLS LAST, a.id DESC",
    "smallest": "r.size_bytes ASC NULLS LAST, a.id DESC",
    "longest": "a.duration_seconds DESC NULLS LAST, a.id DESC",
}

# What a mark says to do with an asset's bytes. `icloud` frees the iCloud copy
# and leaves the drive's; `everywhere` takes both and is the only one that lets
# go of the last copy.
ICLOUD = "icloud"
EVERYWHERE = "everywhere"
MARKS = (ICLOUD, EVERYWHERE)

# Not a mark anything can carry: the filter value for "marked, either way",
# which is the question you actually ask before pruning.
ANY_MARK = "any"
MARK_FILTERS = (ANY_MARK, *MARKS)

# Apple's holding pen, not part of the library. A prune moves an asset here and
# Apple keeps it 30 days, so the next pull files it back under this album with
# no copy on the drive -- a tile the gallery can draw nothing in, for a photo
# you just deleted. Naming the album in the filter is how you look inside it.
TRASH_ALBUM = "Recently Deleted"


@dataclass(frozen=True)
class Filters:
    """Every way the gallery can narrow the library.

    A field left None is not applied at all, which is what makes the unfiltered
    gallery and a filtered one the same code path.
    """

    search: str | None = None
    album: str | None = None
    item_type: str | None = None
    camera_model: str | None = None
    year: int | None = None
    in_icloud: bool | None = None
    backed_up: bool | None = None
    is_favorite: bool | None = None
    has_location: bool | None = None
    # One of `MARK_FILTERS`, so a caller can ask for either level or for both.
    mark: str | None = None
    min_bytes: int | None = None
    min_duration: float | None = None
    max_duration: float | None = None
    sort: str = "newest"


@dataclass(frozen=True)
class Page:
    """One screen of the gallery, plus what it took to build it."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    offset: int = 0

    @property
    def next_offset(self) -> int | None:
        following = self.offset + len(self.rows)
        return following if following < self.total else None


def search(conn: sqlite3.Connection, filters: Filters, *, limit: int = 120, offset: int = 0) -> Page:
    """One page of assets, newest first unless `filters.sort` says otherwise.

    Raises:
        ValueError: If `filters.sort` names no known ordering.
    """
    if filters.sort not in SORTS:
        raise ValueError(f"unknown sort {filters.sort!r}; expected one of {sorted(SORTS)}")

    joins, where, params = _where(filters)
    total = int(conn.execute(f"SELECT count(*) FROM assets a {joins} {where}", params).fetchone()[0])
    rows = conn.execute(
        f"{_GRID_ROW} {where} ORDER BY {SORTS[filters.sort]} LIMIT ? OFFSET ?", (*params, limit, offset)
    ).fetchall()
    return Page(rows=[dict(row) for row in rows], total=total, offset=offset)


def facets(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """The values worth offering as filters, with how many assets each covers.

    Built from the library rather than hard-coded, so a camera that never took
    a photo in it is never offered.
    """
    return {
        "cameras": _counts(
            conn,
            "SELECT camera_model AS value, count(*) AS n FROM assets"
            " WHERE camera_model IS NOT NULL GROUP BY 1 ORDER BY n DESC",
        ),
        "years": _counts(
            conn,
            "SELECT substr(asset_date, 1, 4) AS value, count(*) AS n FROM assets"
            " GROUP BY 1 ORDER BY 1 DESC",
        ),
        "albums": _counts(
            conn, "SELECT source_album AS value, count(*) AS n FROM assets GROUP BY 1 ORDER BY n DESC"
        ),
        "types": _counts(
            conn, "SELECT item_type AS value, count(*) AS n FROM assets GROUP BY 1 ORDER BY n DESC"
        ),
    }


def totals(conn: sqlite3.Connection) -> dict[str, int]:
    """The headline numbers: how much is here, and how much is marked."""
    row = conn.execute("""
        SELECT count(*) AS assets,
               coalesce(sum(a.in_icloud), 0) AS in_icloud,
               coalesce(sum(r.size_bytes), 0) AS bytes
        FROM assets a LEFT JOIN asset_resources r ON r.asset_id = a.id AND r.kind = 'original'
        """).fetchone()
    # Pending marks only: an executed one is history, not a to-do list.
    marked = conn.execute("""
        SELECT m.mark, count(*) AS n, coalesce(sum(r.size_bytes), 0) AS bytes
        FROM marks m
        LEFT JOIN asset_resources r ON r.asset_id = m.asset_id AND r.kind = 'original'
        WHERE m.executed_at IS NULL
        GROUP BY m.mark
        """).fetchall()
    by_mark = {item["mark"]: item for item in marked}
    return {
        "assets": row["assets"],
        "in_icloud": row["in_icloud"],
        "bytes": row["bytes"],
        "marked": sum(item["n"] for item in marked),
        "marked_bytes": sum(item["bytes"] for item in marked),
        **{f"marked_{mark}": by_mark[mark]["n"] if mark in by_mark else 0 for mark in MARKS},
    }


def get_asset(conn: sqlite3.Connection, asset_id: str) -> dict[str, Any] | None:
    """One asset with everything the detail panel shows, or None if unknown."""
    row = conn.execute(f"{_DETAIL_ROW} WHERE a.id = ?", (asset_id,)).fetchone()
    if row is None:
        return None
    # Everything, raw plists included. This is one asset on demand, not the
    # grid, so the exif blob the grid row avoids costs nothing here.
    asset = dict(row)
    for column in ("exif", "people", "keywords"):
        asset[column] = json.loads(asset[column]) if asset[column] else None
    asset["resources"] = [
        dict(item)
        for item in conn.execute(
            "SELECT kind, size_bytes, content_type, width, height FROM asset_resources"
            " WHERE asset_id = ? ORDER BY size_bytes DESC",
            (asset_id,),
        )
    ]
    return asset


def original_path(conn: sqlite3.Connection, asset_id: str) -> str | None:
    """Where this asset's original sits on the drive, or None if it is not backed up."""
    row = conn.execute(
        "SELECT relative_path FROM files WHERE asset_id = ? AND kind = ?",
        (asset_id, library.ORIGINAL),
    ).fetchone()
    return str(row["relative_path"]) if row else None


def set_mark(conn: sqlite3.Connection, asset_id: str, mark: str | None) -> bool:
    """Flag an asset for deletion at one of `MARKS`, or clear the flag with None.

    Records the decision. `prune.py` is what carries it out.

    Re-marking clears `executed_at`: an asset deleted from iCloud and marked
    again is a fresh decision, not the old one repeated.

    Returns:
        Whether the asset exists. False means nothing was written.

    Raises:
        ValueError: If `mark` is neither None nor one of `MARKS`.
    """
    if mark is not None and mark not in MARKS:
        raise ValueError(f"unknown mark {mark!r}; expected one of {list(MARKS)}")
    if conn.execute("SELECT 1 FROM assets WHERE id = ?", (asset_id,)).fetchone() is None:
        return False

    with conn:
        if mark is None:
            conn.execute("DELETE FROM marks WHERE asset_id = ?", (asset_id,))
        else:
            conn.execute(
                "INSERT INTO marks (asset_id, mark, marked_at) VALUES (?, ?, datetime('now'))"
                " ON CONFLICT(asset_id) DO UPDATE SET mark = excluded.mark,"
                " marked_at = excluded.marked_at, executed_at = NULL",
                (asset_id, mark),
            )
    return True


def pending_marks(conn: sqlite3.Connection, mark: str | None = None) -> list[dict[str, Any]]:
    """Every mark still waiting to be carried out, oldest decision first.

    Args:
        mark: Restrict to one of `MARKS`. The default returns both levels.
    """
    where = "AND m.mark = ?" if mark is not None else ""
    rows = conn.execute(
        f"""
        SELECT m.asset_id, m.mark, m.marked_at, a.filename, a.source_album, a.in_icloud,
               a.icloud_bytes, r.size_bytes AS original_bytes
        FROM marks m
        JOIN assets a ON a.id = m.asset_id
        LEFT JOIN asset_resources r ON r.asset_id = m.asset_id AND r.kind = 'original'
        WHERE m.executed_at IS NULL {where}
        ORDER BY m.marked_at, m.asset_id
        """,
        (mark,) if mark is not None else (),
    )
    return [dict(row) for row in rows]


def record_executed(conn: sqlite3.Connection, asset_id: str) -> None:
    """Stamp a mark as carried out, so it stops counting as a to-do."""
    with conn:
        conn.execute("UPDATE marks SET executed_at = datetime('now') WHERE asset_id = ?", (asset_id,))


def _counts(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [{"value": row["value"], "count": row["n"]} for row in conn.execute(sql)]


def _where(filters: Filters) -> tuple[str, str, list[Any]]:
    """The WHERE the page and its count share, as joins, SQL, and parameters.

    A clause that reaches into a joined table names that join itself, so the
    count cannot end up without a table its own filter references.
    """
    clauses: list[str] = []
    joins: set[str] = set()
    params: list[Any] = []

    def add(sql: str, *values: Any, join: str = "") -> None:
        clauses.append(sql)
        params.extend(values)
        if join:
            joins.add(join)

    if filters.search:
        add("a.filename LIKE ?", f"%{filters.search}%")
    if filters.album:
        add("a.source_album = ?", filters.album)
    else:
        add("a.source_album != ?", TRASH_ALBUM)
    if filters.item_type:
        add("a.item_type = ?", filters.item_type)
    if filters.camera_model:
        add("a.camera_model = ?", filters.camera_model)
    if filters.year is not None:
        add("substr(a.asset_date, 1, 4) = ?", str(filters.year))
    if filters.in_icloud is not None:
        add("a.in_icloud = ?", int(filters.in_icloud))
    if filters.is_favorite is not None:
        add("a.is_favorite = ?", int(filters.is_favorite))
    if filters.has_location is not None:
        add("a.latitude IS NOT NULL" if filters.has_location else "a.latitude IS NULL")
    if filters.backed_up is not None:
        add("f.relative_path IS NOT NULL" if filters.backed_up else "f.relative_path IS NULL", join=_FILES)
    if filters.mark == ANY_MARK:
        add("m.mark IS NOT NULL", join=_MARKS)
    elif filters.mark is not None:
        add("m.mark = ?", filters.mark, join=_MARKS)
    if filters.min_bytes is not None:
        add("r.size_bytes >= ?", filters.min_bytes, join=_RESOURCES)
    if filters.min_duration is not None:
        add("a.duration_seconds >= ?", filters.min_duration)
    if filters.max_duration is not None:
        add("a.duration_seconds <= ?", filters.max_duration)

    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return " ".join(sorted(joins)), where, params
