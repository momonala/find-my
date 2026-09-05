"""Refreshing the library's view of iCloud.

Every other command reads what a pull left, so a pull must either land
completely or leave the library as it was. The incremental pull reads only the
front of the Library album, which adds a second property: what it did not read
survives untouched, and what it cannot account for sends it back for a full pull.
"""

import sqlite3
from collections import Counter
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

import pytest

from icloud_photo_metadata import library
from icloud_photo_metadata import pull_metadata
from icloud_photo_metadata.icloud_source import SOURCE_ALBUMS
from icloud_photo_metadata.icloud_source import PageFetchError
from icloud_photo_metadata.models import PhotoMetadata
from icloud_photo_metadata.pull_metadata import refresh_full
from icloud_photo_metadata.pull_metadata import refresh_incremental
from tests.icloud_factories import make_photo


@pytest.fixture
def conn(tmp_path: Any) -> Iterable[sqlite3.Connection]:
    """A library in a temp directory, so no test can touch a real drive."""
    with library.library(tmp_path) as connection:
        yield connection


def ids_in_icloud(conn: sqlite3.Connection) -> set[str]:
    return {photo.id for photo in library.load_assets(conn, in_icloud_only=True)}


def fake_pages(monkeypatch: pytest.MonkeyPatch, *pages: list | Exception) -> None:
    """Serve `pages` as the pull's page fetches, one task per page."""
    monkeypatch.setattr(pull_metadata, "build_page_tasks", lambda *_, **__: list(range(len(pages))))
    monkeypatch.setattr(pull_metadata, "album_length", lambda *_: None)

    def fetch_page(index: int) -> list:
        page = pages[index]
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(pull_metadata, "fetch_page", fetch_page)


def test_a_complete_pull_retires_what_icloud_no_longer_returns(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The asset keeps its row -- this drive may be its only copy -- but leaves iCloud."""
    library.replace_assets(conn, [make_photo("gone", "OLD.MOV")])
    fake_pages(monkeypatch, [make_photo("id-1", "IMG_1.MOV")])

    result = refresh_full(conn, object(), quiet=True)

    assert result.is_complete
    assert result.written == 1
    assert ids_in_icloud(conn) == {"id-1"}
    assert {photo.id for photo in library.load_assets(conn)} == {"id-1", "gone"}


def test_a_pull_that_lost_pages_leaves_the_library_untouched(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """Half a library written over a good one would be worse than a stale one."""
    library.replace_assets(conn, [make_photo("id-1", "IMG_1.MOV")])
    fake_pages(
        monkeypatch,
        [make_photo("id-2", "IMG_2.MOV")],
        PageFetchError("Library offset 100: connection reset"),
    )

    result = refresh_full(conn, object(), quiet=True)

    assert not result.is_complete
    assert result.failed_pages == 1
    assert ids_in_icloud(conn) == {"id-1"}


def test_an_asset_in_two_albums_is_stored_once(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """Otherwise every storage report double-counts it."""
    library_copy = make_photo("id-1", "IMG_1.MOV")
    hidden_copy = make_photo("id-1", "IMG_1.MOV", album="Hidden")
    fake_pages(monkeypatch, [library_copy], [hidden_copy])

    result = refresh_full(conn, object(), quiet=True)

    assert result.written == 1
    [photo] = library.load_assets(conn)
    assert photo.source_album == "Hidden", "the exhaustively crawled album wins the tie"


def fake_albums(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    pages_by_album: Sequence[Sequence[PhotoMetadata] | Exception],
    *,
    hidden: Sequence[PhotoMetadata] = (),
    deleted: Sequence[PhotoMetadata] = (),
    live_counts: Mapping[str, int] | None = None,
    baseline: Mapping[str, int] | None = None,
) -> list[tuple[str, int]]:
    """Serve album pages to the incremental pull, newest page first.

    `pages_by_album` is the whole live Library, page by page. By default iCloud
    reports exactly what those pages hold and the baseline is what the library
    holds, so an unchanged album shows no drift; a test wanting the walk to miss
    something passes `live_counts` the pages do not back up.

    Returns the list each fetched page is appended to, which is how a test sees
    where the walk stopped.
    """
    pages: dict[str, list[Any]] = {
        "Library": list(pages_by_album),
        "Hidden": [list(hidden)] if hidden else [],
        "Recently Deleted": [list(deleted)] if deleted else [],
    }
    fetched: list[tuple[str, int]] = []

    def build_page_tasks(_api: Any, albums: Iterable[str] = SOURCE_ALBUMS) -> list:
        return [(name, index) for name in albums for index in range(len(pages[name]))]

    def fetch_page(task: tuple[str, int]) -> list[PhotoMetadata]:
        fetched.append(task)
        page = pages[task[0]][task[1]]
        if isinstance(page, Exception):
            raise page
        return list(page)

    counts = {
        name: sum(len(page) for page in album if not isinstance(page, Exception))
        for name, album in pages.items()
    }
    counts.update(live_counts or {})

    if baseline is None:
        held = library.load_assets(conn, in_icloud_only=True)
        baseline = {name: 0 for name in SOURCE_ALBUMS} | Counter(photo.source_album for photo in held)
    library.save_album_counts(conn, dict(baseline))

    monkeypatch.setattr(pull_metadata, "build_page_tasks", build_page_tasks)
    monkeypatch.setattr(pull_metadata, "fetch_page", fetch_page)
    monkeypatch.setattr(pull_metadata, "album_length", lambda _api, name: counts.get(name))
    return fetched


def stub_full_pull(monkeypatch: pytest.MonkeyPatch, written: int = 99) -> list[dict[str, Any]]:
    """Record every escalation to a full pull instead of running one."""
    calls: list[dict[str, Any]] = []

    def refresh(*_: Any, **kwargs: Any) -> pull_metadata.PullResult:
        calls.append(kwargs)
        return pull_metadata.PullResult(written, 0)

    monkeypatch.setattr(pull_metadata, "refresh_full", refresh)
    return calls


def test_the_walk_stops_once_pages_stop_carrying_new_ids(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The whole point: a library of many pages costs a handful of requests."""
    old = [make_photo(f"id-{n}", f"IMG_{n}.HEIC") for n in range(5)]
    library.replace_assets(conn, old)
    fresh = make_photo("id-new", "IMG_NEW.HEIC")
    fetched = fake_albums(monkeypatch, conn, [[fresh, old[0]], [old[1]], [old[2]], [old[3]], [old[4]]])

    result = refresh_incremental(conn, object(), quiet=True)

    assert result.is_complete
    assert fetched == [("Library", 0), ("Library", 1), ("Library", 2)]
    assert ids_in_icloud(conn) == {"id-new", *(photo.id for photo in old)}


def test_assets_the_walk_never_reached_survive(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """An incremental pull merges; it must not truncate the library to what it read."""
    deep = make_photo("id-deep", "IMG_DEEP.HEIC")
    library.replace_assets(conn, [deep, make_photo("id-1", "IMG_1.HEIC")])
    fake_albums(monkeypatch, conn, [[make_photo("id-1", "IMG_1.HEIC")], [deep]])

    refresh_incremental(conn, object(), quiet=True)

    assert "id-deep" in ids_in_icloud(conn)


def test_an_asset_moved_to_recently_deleted_is_reclassified(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """Every pull overwrites what it reads, so an album can never go stale."""
    library.replace_assets(conn, [make_photo("id-1", "IMG_1.MOV"), make_photo("id-2", "IMG_2.MOV")])
    fake_albums(
        monkeypatch,
        conn,
        [[make_photo("id-2", "IMG_2.MOV")]],
        deleted=[make_photo("id-1", "IMG_1.MOV", album="Recently Deleted")],
    )

    refresh_incremental(conn, object(), quiet=True)

    albums = {photo.id: photo.source_album for photo in library.load_assets(conn)}
    assert albums == {"id-1": "Recently Deleted", "id-2": "Library"}


def test_an_asset_gone_from_a_small_album_leaves_icloud_but_keeps_its_row(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """Hidden and Recently Deleted are crawled in full, so their crawl is the truth.

    It says the asset has left iCloud, not that it never existed: the drive may
    hold its only copy.
    """
    library.replace_assets(
        conn,
        [make_photo("id-1", "IMG_1.MOV"), make_photo("id-purged", "OLD.MOV", album="Recently Deleted")],
    )
    fake_albums(monkeypatch, conn, [[make_photo("id-1", "IMG_1.MOV")]])

    refresh_incremental(conn, object(), quiet=True)

    assert ids_in_icloud(conn) == {"id-1"}
    assert {photo.id for photo in library.load_assets(conn)} == {"id-1", "id-purged"}


def test_a_change_the_walk_did_not_account_for_escalates_to_a_full_pull(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """A change too deep for the walk to reach must not be written as a silent gap.

    iCloud reports one more Library asset than it did last run, but the pages
    the walk reads hold no id the library has not already seen -- the signature
    of an import whose capture date sorted it past them.
    """
    library.replace_assets(conn, [make_photo("id-1", "IMG_1.HEIC")])
    fake_albums(
        monkeypatch,
        conn,
        [[make_photo("id-1", "IMG_1.HEIC")]],
        live_counts={"Library": 2},
        baseline={"Library": 1, "Hidden": 0, "Recently Deleted": 0},
    )
    escalations = stub_full_pull(monkeypatch)

    result = refresh_incremental(conn, object(), quiet=True)

    assert escalations
    assert result.written == 99, "the full pull's result is what the caller gets"


def test_an_empty_library_falls_back_to_a_full_pull(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """There is nothing to merge into on the first ever run."""
    fake_albums(monkeypatch, conn, [[make_photo("id-1", "IMG_1.HEIC")]])
    escalations = stub_full_pull(monkeypatch, written=1)

    refresh_incremental(conn, object(), quiet=True)

    assert escalations


def test_an_incremental_pull_that_lost_a_page_leaves_the_library_untouched(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """Merging over a hole would drop whatever that page was carrying."""
    library.replace_assets(conn, [make_photo("id-1", "IMG_1.MOV")])
    fake_albums(monkeypatch, conn, [PageFetchError("Library offset 0: connection reset")])

    result = refresh_incremental(conn, object(), quiet=True)

    assert not result.is_complete
    assert ids_in_icloud(conn) == {"id-1"}


def test_a_reported_length_that_overcounts_is_not_read_as_drift(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The guard compares deltas precisely because absolutes never line up.

    A real album reports more rows than its pages yield, so a reported length
    checked against a record count would escalate on every single run.
    """
    photos = [make_photo("id-1", "IMG_1.HEIC"), make_photo("id-2", "IMG_2.HEIC")]
    library.replace_assets(conn, photos)
    fake_albums(
        monkeypatch,
        conn,
        [photos],
        live_counts={"Library": 4},
        baseline={"Library": 4, "Hidden": 0, "Recently Deleted": 0},
    )
    escalations = stub_full_pull(monkeypatch)

    result = refresh_incremental(conn, object(), quiet=True)

    assert escalations == [], "an album reporting 4 where its pages yield 2 is not drift"
    assert result.written == 2


def test_no_baseline_falls_back_to_a_full_pull(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """Without a record of what iCloud last said, drift cannot be measured at all."""
    library.replace_assets(conn, [make_photo("id-1", "IMG_1.HEIC")])
    fake_albums(monkeypatch, conn, [[make_photo("id-1", "IMG_1.HEIC")]])
    conn.execute("DELETE FROM album_counts")
    escalations = stub_full_pull(monkeypatch, written=1)

    refresh_incremental(conn, object(), quiet=True)

    assert escalations


def test_a_pull_records_what_icloud_reported_for_the_next_one(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The baseline has to advance, or every later run measures drift from the same point."""
    library.replace_assets(conn, [make_photo("id-1", "IMG_1.HEIC")])
    fresh = make_photo("id-2", "IMG_2.HEIC")
    fake_albums(monkeypatch, conn, [[fresh, make_photo("id-1", "IMG_1.HEIC")]])

    refresh_incremental(conn, object(), quiet=True)

    counts = library.load_album_counts(conn)
    assert counts is not None
    assert counts["Library"] == 2


class RecordingBar:
    """A stand-in for tqdm that remembers the largest total it ever advertised."""

    def __init__(self, *_: Any, total: int = 0, **__: Any) -> None:
        self.total = total
        self.updates = 0
        self.high_water = total

    def __enter__(self) -> "RecordingBar":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def update(self, n: int) -> None:
        self.updates += n

    def refresh(self) -> None:
        self.high_water = max(self.high_water, self.total)

    def set_postfix(self, **_: Any) -> None:
        return None

    @staticmethod
    def write(*_: Any, **__: Any) -> None:
        return None


def test_the_progress_bar_never_promises_pages_it_will_not_read(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """A bar sized to the whole task list would advertise ~99 pages and an ETA
    to match, then stop at six -- which reads as a stalled full pull."""
    old = [make_photo(f"id-{n}", f"IMG_{n}.HEIC") for n in range(6)]
    library.replace_assets(conn, old)
    fake_albums(monkeypatch, conn, [[old[0]], [old[1]], [old[2]], [old[3]], [old[4]], [old[5]]])
    bars: list[RecordingBar] = []

    def make_bar(*args: Any, **kwargs: Any) -> RecordingBar:
        bar = RecordingBar(*args, **kwargs)
        bars.append(bar)
        return bar

    monkeypatch.setattr(pull_metadata, "tqdm", make_bar)

    result = refresh_incremental(conn, object(), quiet=True)

    assert result.pages_read == 2, "settles after two all-known pages"
    assert bars[0].high_water == result.pages_read, "advertised exactly what it read"
    assert bars[0].updates == result.pages_read
