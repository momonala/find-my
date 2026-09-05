"""Pull iCloud Photos metadata into the library on the drive. Never the bytes.

Every page costs the same ~3s whatever it holds -- the cost is Apple's round
trip, not the payload -- and `_SESSION_LOCK` serializes those trips anyway, so
page count is the only lever: `refresh_full` reads every page,
`refresh_incremental` reads the front of the Library until pages stop carrying
unseen ids.

Both crawl the two small albums exhaustively, so every asset's `source_album`
says where it is now rather than where it was first seen.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import typer
from tqdm import tqdm

from icloud_photo_metadata import library
from icloud_photo_metadata.icloud_source import LIBRARY_ALBUM
from icloud_photo_metadata.icloud_source import SMALL_ALBUMS
from icloud_photo_metadata.icloud_source import SOURCE_ALBUMS
from icloud_photo_metadata.icloud_source import PageFetchError
from icloud_photo_metadata.icloud_source import PageTask
from icloud_photo_metadata.icloud_source import album_length
from icloud_photo_metadata.icloud_source import build_page_tasks
from icloud_photo_metadata.icloud_source import fetch_page
from icloud_photo_metadata.models import PhotoMetadata

# One all-known page would almost always end the walk correctly; the second is
# margin against a burst straddling a page boundary, bought for three seconds.
_SETTLE_PAGES = 2


@dataclass(frozen=True)
class PullResult:
    """How a refresh went."""

    written: int
    failed_pages: int
    pages_read: int = 0

    @property
    def is_complete(self) -> bool:
        return self.failed_pages == 0


def refresh_full(conn: sqlite3.Connection, api: Any, *, quiet: bool = False) -> PullResult:
    """Re-pull every source album, and retire whatever it does not find.

    Nothing is written until every page has succeeded.

    Returns:
        A result that is not `is_complete` left the library untouched.
    """
    tasks = build_page_tasks(api)
    # Before the crawl: a change landing mid-crawl must read as drift next run,
    # not as a baseline this library never contained.
    reported = _album_lengths(api)
    if not quiet:
        for album_name, pages in Counter(task.album_name for task in tasks).items():
            typer.echo(f"{album_name}: {pages} pages")

    with tqdm(total=len(tasks), unit="page", desc="Fetching pages", disable=quiet or None) as bar:
        photos, read, failed = _fetch_all(tasks, bar=bar)
    if failed:
        return PullResult(written=0, failed_pages=failed, pages_read=read)

    written = library.replace_assets(conn, _deduplicate(photos))
    library.save_album_counts(conn, reported)
    return PullResult(written=written, failed_pages=0, pages_read=read)


def refresh_incremental(conn: sqlite3.Connection, api: Any, *, quiet: bool = False) -> PullResult:
    """Refresh from the front of the library, merging over what it already holds.

    Falls back to `refresh_full` with nothing to merge into or compare against,
    or when an album moved at iCloud by more than the merge accounts for --
    which is how a change too deep for the walk to reach announces itself.

    Returns:
        A result that is not `is_complete` left the library untouched.
    """
    known = library.known_ids(conn)
    if not known:
        if not quiet:
            typer.echo("The library is empty; pulling in full.")
        return refresh_full(conn, api, quiet=quiet)

    baseline = library.load_album_counts(conn)
    if baseline is None:
        if not quiet:
            typer.echo("No record of what iCloud last reported; pulling in full to establish one.")
        return refresh_full(conn, api, quiet=quiet)

    # Before the walk, for the reason `refresh_full` gives.
    reported = _album_lengths(api)
    small_tasks = build_page_tasks(api, SMALL_ALBUMS)
    library_tasks = build_page_tasks(api, (LIBRARY_ALBUM,))

    # Only the pages committed to so far; the walk extends it a chunk at a time.
    # Sized to the task list the bar would promise ~99 and stop at six.
    with tqdm(total=len(small_tasks), unit="page", desc="Reading changes", disable=quiet or None) as bar:
        fresh_small, small_read, small_failed = _fetch_all(small_tasks, bar=bar)
        fresh_library, library_read, library_failed = _walk_until_settled(library_tasks, known, bar=bar)

    pages_read = small_read + library_read
    failed = small_failed + library_failed
    if failed:
        return PullResult(written=0, failed_pages=failed, pages_read=pages_read)

    before = library.live_album_counts(conn)
    written = library.merge_assets(
        conn, _deduplicate([*fresh_library, *fresh_small]), exhaustive_albums=SMALL_ALBUMS
    )
    drift = _unexplained_drift(baseline, reported, before, library.live_album_counts(conn))
    if drift is not None:
        # The merge stands rather than rolling back: the full pull overwrites
        # it, and if that fails the unsaved baseline re-detects the same drift.
        if not quiet:
            typer.echo(f"{drift} Pulling in full.")
        return refresh_full(conn, api, quiet=quiet)

    library.save_album_counts(conn, reported)
    return PullResult(written=written, failed_pages=0, pages_read=pages_read)


def _deduplicate(photos: Sequence[PhotoMetadata]) -> list[PhotoMetadata]:
    """One record per asset, since an asset can be listed in two albums.

    A tie goes to the small album, crawled end to end and so the more
    trustworthy. Ranked rather than by arrival: pages are fetched concurrently.
    """
    ranked = sorted(photos, key=lambda photo: photo.source_album in SMALL_ALBUMS)
    return list({photo.id: photo for photo in ranked}.values())


def _album_lengths(api: Any) -> dict[str, int]:
    """What iCloud reports for each source album, skipping any that is gone."""
    counts = {name: album_length(api, name) for name in SOURCE_ALBUMS}
    return {name: count for name, count in counts.items() if count is not None}


def _unexplained_drift(
    baseline: Mapping[str, int],
    reported: Mapping[str, int],
    before: Mapping[str, int],
    after: Mapping[str, int],
) -> str | None:
    """Say which album moved at iCloud by more than the merge accounts for.

    Deltas, never absolutes: an album's reported length counts rows its pages
    never return (9,620 against 9,618 here), so it cannot be checked against a
    record count. That offset is stable between runs, which leaves the delta a
    faithful signal.

    Returns a sentence naming the album and both figures, or None.
    """
    for name, count in reported.items():
        if name not in baseline:
            return f"No record of what iCloud last reported for {name}."
        at_icloud = count - baseline[name]
        in_merge = after.get(name, 0) - before.get(name, 0)
        if at_icloud != in_merge:
            return (
                f"{name} moved by {at_icloud:+,} at iCloud but by {in_merge:+,} in the merge; "
                "something changed deeper than the walk reached."
            )
    return None


def _read_page(task: PageTask) -> list[PhotoMetadata] | None:
    """One page, or None if it could not be read after its retries."""
    try:
        return fetch_page(task)
    except PageFetchError as error:
        tqdm.write(f"  ! {error}", file=sys.stderr)
        return None


def _fetch_all(tasks: Sequence[PageTask], *, bar: tqdm) -> tuple[list[PhotoMetadata], int, int]:
    """Fetch every task, returning its records, pages read, and pages failed."""
    photos: list[PhotoMetadata] = []
    read = failed = 0
    for task in tasks:
        page = _read_page(task)
        if page is None:
            failed += 1
        else:
            read += 1
            photos.extend(page)
        bar.update(1)
        bar.set_postfix(records=len(photos), failed=failed)
    return photos, read, failed


def _walk_until_settled(
    tasks: Sequence[PageTask], known: set[str], *, bar: tqdm
) -> tuple[list[PhotoMetadata], int, int]:
    """Read `tasks` from the front until pages stop carrying unseen ids.

    Whole pages are kept, not just their unseen records -- that is how an edit
    near the top of the library reaches it.
    """
    photos: list[PhotoMetadata] = []
    settled = read = failed = 0
    for task in tasks:
        bar.total += 1
        bar.refresh()
        page = _read_page(task)
        bar.update(1)
        if page is None:
            # A lost page makes the walk moot: the caller discards the run
            # rather than merge a hole.
            return photos, read, failed + 1
        read += 1
        settled = 0 if any(photo.id not in known for photo in page) else settled + 1
        photos.extend(page)
        if settled >= _SETTLE_PAGES:
            break
    return photos, read, failed
