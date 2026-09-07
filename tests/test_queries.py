"""Reads that back the gallery: filtering, paging, facets, and marking.

The filters matter because they are the whole point of the database, and
marking matters because it is the only thing in the library a person authors.
"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from icloud_photo_metadata import library
from icloud_photo_metadata import queries
from icloud_photo_metadata.library import BackedUpFile
from icloud_photo_metadata.queries import Filters
from tests.icloud_factories import make_photo


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with library.library(tmp_path) as connection:
        yield connection


def stock(conn: sqlite3.Connection) -> None:
    """A small library spanning the dimensions the gallery filters on.

    Every asset gets a distinct date and size, so an assertion about ordering
    is about the ordering rather than about how ties happen to break.
    """
    library.replace_assets(
        conn,
        [
            make_photo(
                "still-2024", "IMG_1.HEIC", size=6_000_000, captured_at=datetime(2024, 6, 1, tzinfo=UTC)
            ),
            make_photo(
                "clip",
                "IMG_3.MOV",
                size=9_000,
                item_type="movie",
                captured_at=datetime(2024, 3, 1, tzinfo=UTC),
            ),
            make_photo(
                "hidden",
                "IMG_4.HEIC",
                size=2_000,
                album="Hidden",
                captured_at=datetime(2024, 1, 1, tzinfo=UTC),
            ),
            make_photo("still-2023", "IMG_2.HEIC", size=1_000, captured_at=datetime(2023, 5, 1, tzinfo=UTC)),
        ],
    )
    conn.execute("UPDATE assets SET duration_seconds = 42 WHERE id = 'clip'")
    conn.execute(
        "UPDATE assets SET is_favorite = 1, latitude = 52.5, longitude = 13.4 WHERE id = 'still-2024'"
    )
    conn.commit()


def ids(conn: sqlite3.Connection, filters: Filters) -> list[str]:
    return [row["id"] for row in queries.search(conn, filters).rows]


def test_the_unfiltered_gallery_is_every_asset_newest_first(conn: sqlite3.Connection) -> None:
    stock(conn)

    assert ids(conn, Filters()) == ["still-2024", "clip", "hidden", "still-2023"]


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (Filters(item_type="movie"), ["clip"]),
        (Filters(album="Hidden"), ["hidden"]),
        (Filters(year=2023), ["still-2023"]),
        (Filters(is_favorite=True), ["still-2024"]),
        (Filters(has_location=True), ["still-2024"]),
        (Filters(min_bytes=5_000_000), ["still-2024"]),
        (Filters(min_duration=10), ["clip"]),
        (Filters(max_duration=10), []),
        (Filters(search="IMG_3"), ["clip"]),
        (Filters(sort="largest"), ["still-2024", "clip", "hidden", "still-2023"]),
        (Filters(sort="oldest"), ["still-2023", "hidden", "clip", "still-2024"]),
    ],
    ids=lambda value: getattr(value, "sort", None) if isinstance(value, Filters) else "",
)
def test_each_filter_narrows_to_what_it_names(
    conn: sqlite3.Connection, filters: Filters, expected: list[str]
) -> None:
    stock(conn)

    assert ids(conn, filters) == expected


def test_an_unknown_sort_is_refused_rather_than_interpolated(conn: sqlite3.Connection) -> None:
    """The sort reaches SQL as text, so anything unrecognised stops here."""
    with pytest.raises(ValueError, match="unknown sort"):
        queries.search(conn, Filters(sort="'; DROP TABLE assets --"))


def test_a_freed_asset_is_filterable_apart_from_a_live_one(conn: sqlite3.Connection) -> None:
    """Assets this drive is the only copy of are worth seeing on their own."""
    library.replace_assets(conn, [make_photo("live", "IMG_1.HEIC")])
    library.restore_assets(conn, [make_photo("freed", "IMG_2.HEIC")])

    assert ids(conn, Filters(in_icloud=True)) == ["live"]
    assert ids(conn, Filters(in_icloud=False)) == ["freed"]


def test_assets_with_no_file_on_the_drive_are_filterable(conn: sqlite3.Connection) -> None:
    library.replace_assets(conn, [make_photo("kept", "IMG_1.HEIC"), make_photo("unbacked", "IMG_2.HEIC")])
    library.record_files(
        conn,
        "kept",
        [BackedUpFile(library.ORIGINAL, "2024/IMG_1.HEIC", 1000, "hash")],
        recorded_as="downloaded",
    )

    assert ids(conn, Filters(backed_up=True)) == ["kept"]
    assert ids(conn, Filters(backed_up=False)) == ["unbacked"]


def test_a_page_says_where_the_next_one_starts_and_when_there_is_none(conn: sqlite3.Connection) -> None:
    stock(conn)

    first = queries.search(conn, Filters(), limit=3)
    last = queries.search(conn, Filters(), limit=3, offset=3)

    assert (first.total, first.next_offset) == (4, 3)
    assert (len(last.rows), last.next_offset) == (1, None)


def test_a_grid_row_never_carries_the_exif_blob(conn: sqlite3.Connection) -> None:
    """Reading 34 MB of plists off the drive costs a grid page a second."""
    stock(conn)

    [row] = queries.search(conn, Filters(search="IMG_1")).rows

    assert "exif" not in row
    assert "camera_model" not in row


def test_the_detail_view_carries_everything_including_the_raw_plists(conn: sqlite3.Connection) -> None:
    """The panel shows all of an asset's metadata, so nothing may be held back.

    Unlike a grid row this is one asset on demand, which is what makes the exif
    blob affordable here and not there.
    """
    exif = {"{TIFF}": {"Model": "iPhone 13"}}
    photo = make_photo("id-1", "IMG_1.HEIC").model_copy(update={"exif": exif})
    library.replace_assets(conn, [photo])

    asset = queries.get_asset(conn, "id-1")

    assert asset is not None
    assert asset["camera_model"] == "iPhone 13"
    assert asset["exif"] == exif, "decoded, not left as a JSON string"
    assert {"resources", "relative_path", "mark", "in_icloud"} <= set(asset)


def test_an_unknown_asset_has_no_detail(conn: sqlite3.Connection) -> None:
    assert queries.get_asset(conn, "never-existed") is None


def test_facets_offer_only_values_the_library_actually_holds(conn: sqlite3.Connection) -> None:
    stock(conn)

    found = queries.facets(conn)

    assert {item["value"] for item in found["albums"]} == {"Library", "Hidden"}
    assert {item["value"] for item in found["types"]} == {"image", "movie"}
    assert found["years"][0] == {"value": "2024", "count": 3}


def test_marking_an_asset_survives_and_can_be_cleared(conn: sqlite3.Connection) -> None:
    stock(conn)

    assert queries.set_mark(conn, "clip", "icloud") is True
    assert ids(conn, Filters(mark="icloud")) == ["clip"]

    queries.set_mark(conn, "clip", None)

    assert ids(conn, Filters(mark="icloud")) == []


def test_re_marking_replaces_rather_than_duplicates(conn: sqlite3.Connection) -> None:
    stock(conn)

    queries.set_mark(conn, "clip", "icloud")
    queries.set_mark(conn, "clip", "everywhere")

    assert conn.execute("SELECT count(*) FROM marks").fetchone()[0] == 1
    assert ids(conn, Filters(mark="everywhere")) == ["clip"]


def test_marking_an_asset_the_library_does_not_know_writes_nothing(conn: sqlite3.Connection) -> None:
    """The table has a foreign key, so a bad id must be refused, not raised over."""
    assert queries.set_mark(conn, "never-existed", "icloud") is False
    assert conn.execute("SELECT count(*) FROM marks").fetchone()[0] == 0


def test_an_unknown_mark_is_refused(conn: sqlite3.Connection) -> None:
    stock(conn)

    with pytest.raises(ValueError, match="unknown mark"):
        queries.set_mark(conn, "clip", "burn")


def test_totals_report_the_library_and_what_is_marked(conn: sqlite3.Connection) -> None:
    stock(conn)
    queries.set_mark(conn, "still-2024", "icloud")

    found = queries.totals(conn)

    assert found["assets"] == 4
    assert found["marked"] == 1
    assert found["marked_bytes"] == 6_000_000
