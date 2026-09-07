"""The library is the drive's durable record, so these pin what must not slip.

An asset leaving iCloud keeps its row -- that is what a one-way backup means --
and a round trip through the schema returns the model it was handed.
"""

import sqlite3
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from icloud_photo_metadata import library
from icloud_photo_metadata.library import BackedUpFile
from icloud_photo_metadata.models import GpsFix
from icloud_photo_metadata.models import PhotoMetadata
from icloud_photo_metadata.models import StoredResource
from tests.icloud_factories import CAPTURED_AT
from tests.icloud_factories import make_photo


def fully_populated() -> PhotoMetadata:
    """An asset with every optional field set, including the awkward shapes."""
    return PhotoMetadata(
        id="asset-1",
        master_id="master-1",
        filename="IMG_1.HEIC",
        source_album="Library",
        item_type="image",
        is_live_photo=True,
        asset_subtype_v2=3,
        hdr_type=0,
        burst_flags=1,
        data_class_type=1,
        resources={
            "original": StoredResource(
                size_bytes=1000, content_type="public.heic", width=4032, height=3024, checksum="AQ=="
            ),
            "original_video": StoredResource(size_bytes=500, content_type="com.apple.quicktime-movie"),
        },
        total_storage_bytes=1500,
        asset_date=CAPTURED_AT,
        added_date=CAPTURED_AT,
        original_creation_date=CAPTURED_AT,
        import_date=CAPTURED_AT,
        master_modified=CAPTURED_AT,
        asset_modified=CAPTURED_AT,
        time_zone_name="Europe/Berlin",
        time_zone_offset_seconds=7200,
        duration_seconds=2.5,
        video_frame_rate=30.0,
        orientation=1,
        original_orientation=1,
        is_favorite=True,
        is_hidden=False,
        rating=-1,
        title="A title",
        description="A description",
        keywords=["holiday", "berlin"],
        people=[{"name": "someone"}],
        view_count=3,
        play_count=1,
        share_count=0,
        imported_by_bundle_id="com.apple.springboard",
        imported_by_display_name="SpringBoard",
        location=GpsFix(latitude=52.55, longitude=13.39, altitude=53.1, speed=0.0, timestamp=CAPTURED_AT),
        exif={"{TIFF}": {"Make": "Apple", "Model": "iPhone 13"}, "{Exif}": {"FNumber": 1.6}},
    )


def test_an_asset_survives_a_round_trip_unchanged(tmp_path: Path) -> None:
    """A field lost here is lost for good once iCloud no longer holds the asset."""
    photo = fully_populated()

    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [photo])

        assert library.load_assets(conn) == [photo]


def test_an_asset_that_leaves_icloud_keeps_its_row(tmp_path: Path) -> None:
    """The drive is the only copy at that point, so forgetting it loses everything."""
    kept, freed = make_photo("id-1", "IMG_1.HEIC"), make_photo("id-2", "IMG_2.HEIC")

    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [kept, freed])
        live = library.replace_assets(conn, [kept])

        assert live == 1
        assert {photo.id for photo in library.load_assets(conn)} == {"id-1", "id-2"}
        assert [photo.id for photo in library.load_assets(conn, in_icloud_only=True)] == ["id-1"]


def test_the_departure_time_is_when_it_went_not_when_it_was_last_missed(tmp_path: Path) -> None:
    photo = make_photo("id-1", "IMG_1.HEIC")

    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [photo])
        library.replace_assets(conn, [])
        first = conn.execute("SELECT icloud_gone_at FROM assets").fetchone()[0]
        library.replace_assets(conn, [])

        assert conn.execute("SELECT icloud_gone_at FROM assets").fetchone()[0] == first


def test_an_asset_recovered_from_recently_deleted_comes_back(tmp_path: Path) -> None:
    """Restoring in the Photos app must undo the departure, not leave a tombstone."""
    photo = make_photo("id-1", "IMG_1.HEIC")

    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [photo])
        first_seen = conn.execute("SELECT first_seen_at FROM assets").fetchone()[0]
        library.replace_assets(conn, [])
        library.replace_assets(conn, [photo])

        row = conn.execute("SELECT in_icloud, icloud_gone_at, first_seen_at FROM assets").fetchone()
        assert (row["in_icloud"], row["icloud_gone_at"]) == (1, None)
        assert row["first_seen_at"] == first_seen


def test_a_partial_crawl_only_retires_assets_from_the_albums_it_read_whole(tmp_path: Path) -> None:
    """Silence about a Library asset means the walk stopped, not that it is gone."""
    unread = make_photo("id-library", "IMG_1.HEIC", album="Library")
    emptied = make_photo("id-hidden", "IMG_2.HEIC", album="Hidden")

    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [unread, emptied])
        live = library.merge_assets(conn, [], exhaustive_albums=["Hidden"])

        assert live == 1
        assert [photo.id for photo in library.load_assets(conn, in_icloud_only=True)] == ["id-library"]


def test_re_pulling_an_asset_replaces_its_resources_rather_than_adding_to_them(tmp_path: Path) -> None:
    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [make_photo("id-1", "IMG_1.HEIC", live_size=500)])
        library.replace_assets(conn, [make_photo("id-1", "IMG_1.HEIC")])

        [photo] = library.load_assets(conn)
        assert set(photo.resources) == {"original"}


def test_a_live_photo_is_one_asset_claiming_two_files(tmp_path: Path) -> None:
    """Two rows for one Live Photo would break the keying the store rests on."""
    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [make_photo("id-1", "IMG_1.HEIC", live_size=500)])
        library.record_files(
            conn,
            "id-1",
            [
                BackedUpFile(library.ORIGINAL, "2024/IMG_1~id-1.HEIC", 1000, "still-hash"),
                BackedUpFile(library.LIVE_VIDEO, "2024/IMG_1~id-1.MOV", 500, "motion-hash"),
            ],
            recorded_as="downloaded",
        )

        [asset] = library.backed_up(conn).values()
        assert asset.total_bytes == 1500
        assert asset.paths(tmp_path) == [tmp_path / "2024/IMG_1~id-1.HEIC", tmp_path / "2024/IMG_1~id-1.MOV"]


def test_recording_a_file_twice_updates_the_one_row(tmp_path: Path) -> None:
    """A re-download after damage must not leave two rows claiming one asset."""
    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [make_photo("id-1", "IMG_1.HEIC")])
        for digest in ("first", "second"):
            library.record_files(
                conn,
                "id-1",
                [BackedUpFile(library.ORIGINAL, "2024/IMG_1~id-1.HEIC", 1000, digest)],
                recorded_as="downloaded",
            )

        [asset] = library.backed_up(conn).values()
        assert [item.sha256 for item in asset.files] == ["second"]


def test_album_counts_round_trip_and_start_empty(tmp_path: Path) -> None:
    """No baseline is what tells the next pull it has to be a full one."""
    with library.library(tmp_path) as conn:
        assert library.load_album_counts(conn) is None

        library.save_album_counts(conn, {"Library": 9620, "Hidden": 96})

        assert library.load_album_counts(conn) == {"Library": 9620, "Hidden": 96}


def test_a_pull_records_when_it_happened(tmp_path: Path) -> None:
    with library.library(tmp_path) as conn:
        assert library.pulled_at(conn) is None

        library.replace_assets(conn, [make_photo("id-1", "IMG_1.HEIC")])

        pulled = library.pulled_at(conn)
        assert pulled is not None
        assert (datetime.now(UTC) - pulled).total_seconds() < 60


def test_a_v1_library_is_migrated_rather_than_refused(tmp_path: Path) -> None:
    """The drive's library is the durable record, so a version bump migrates it.

    v1's single `delete` mark meant what v2 calls `icloud`: free the iCloud copy
    and keep the drive's. Reading it as `everywhere` would turn a decision about
    Apple's disk into one about the only remaining copy.
    """
    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [fully_populated()])
        conn.execute("DROP TABLE marks")
        conn.executescript("""
            CREATE TABLE marks (
                asset_id  TEXT PRIMARY KEY REFERENCES assets(id),
                mark      TEXT NOT NULL CHECK (mark IN ('delete', 'keep')),
                marked_at TEXT NOT NULL
            );
            INSERT INTO marks VALUES ('asset-1', 'delete', '2026-01-01 00:00:00');
            PRAGMA user_version=1;
            """)
        conn.commit()

    with library.library(tmp_path) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM marks")]

    assert rows == [
        {
            "asset_id": "asset-1",
            "mark": "icloud",
            "marked_at": "2026-01-01 00:00:00",
            "executed_at": None,
        }
    ]


def test_a_read_only_open_migrates_a_library_that_is_behind(tmp_path: Path) -> None:
    """A page view cannot write, and reading v1 rows as v2 would misreport them."""
    library.connect(tmp_path).close()
    conn = sqlite3.connect(tmp_path / library.LIBRARY_NAME)
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()

    with library.reading(tmp_path) as reader:
        assert int(reader.execute("PRAGMA user_version").fetchone()[0]) == library.SCHEMA_VERSION


def test_a_library_from_another_schema_refuses_to_open(tmp_path: Path) -> None:
    """Opening it anyway would write rows the older tool cannot read back."""
    library.connect(tmp_path).close()
    conn = sqlite3.connect(tmp_path / library.LIBRARY_NAME)
    conn.execute(f"PRAGMA user_version={library.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(library.SchemaMismatch):
        library.connect(tmp_path)


def test_the_dump_restores_a_library_that_can_be_read_back(tmp_path: Path) -> None:
    """A dump that cannot rebuild the library is not a backup of anything."""
    photo = fully_populated()
    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [photo])
        library.record_files(
            conn,
            "asset-1",
            [BackedUpFile(library.ORIGINAL, "2024/IMG_1.HEIC", 1000, "a-hash")],
            recorded_as="downloaded",
        )
        library.write_dump(conn, tmp_path)

    restored = tmp_path / "restored"
    restored.mkdir()
    sql = (tmp_path / library.DUMP_NAME).read_text()
    sqlite3.connect(restored / library.LIBRARY_NAME).executescript(sql)

    with library.library(restored) as conn:
        assert library.load_assets(conn) == [photo]
        assert library.backed_up(conn)["asset-1"].files[0].sha256 == "a-hash"


def test_the_dump_replaces_the_previous_one_and_leaves_no_temp_file(tmp_path: Path) -> None:
    with library.library(tmp_path) as conn:
        library.write_dump(conn, tmp_path)
        library.replace_assets(conn, [make_photo("id-1", "IMG_1.HEIC")])
        library.write_dump(conn, tmp_path)

    assert "id-1" in (tmp_path / library.DUMP_NAME).read_text()
    assert not list(tmp_path.glob("*.tmp"))


def test_filters_the_ui_needs_read_straight_off_the_schema(tmp_path: Path) -> None:
    """Camera and aperture live inside the EXIF blob; generated columns expose them."""
    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [fully_populated()])

        row = conn.execute(
            "SELECT camera_model, f_number, latitude FROM assets WHERE camera_make = 'Apple'"
        ).fetchone()
        assert (row["camera_model"], row["f_number"], row["latitude"]) == ("iPhone 13", 1.6, 52.55)
