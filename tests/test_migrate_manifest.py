"""The manifest import moves data nothing else can rebuild, so it verifies itself.

These cover what that verification is for: file rows arriving intact, an asset
only the manifest knows about filed as backup-only, and the check itself failing
when the library does not match the JSON.
"""

from pathlib import Path

import pytest

from icloud_photo_metadata import library
from icloud_photo_metadata import migrate_manifest
from icloud_photo_metadata.migrate_manifest import MANIFEST_NAME
from icloud_photo_metadata.migrate_manifest import LegacyManifest
from icloud_photo_metadata.migrate_manifest import LegacyRecord
from icloud_photo_metadata.models import PhotoMetadata
from tests.icloud_factories import CAPTURED_AT
from tests.icloud_factories import make_photo


def record_for(photo: PhotoMetadata) -> LegacyRecord:
    """The manifest record a completed download would have written for `photo`."""
    live = photo.live_video
    return LegacyRecord(
        relative_path=f"2024/{photo.filename}",
        size_bytes=photo.original_size_bytes or 0,
        sha256=f"hash-of-{photo.id}",
        recorded_at=CAPTURED_AT,
        recorded_as="downloaded",
        metadata=photo.model_dump(mode="json"),
        live_relative_path=f"2024/{photo.id}.MOV" if live else None,
        live_size_bytes=photo.live_video_size_bytes if live else None,
        live_sha256=f"motion-of-{photo.id}" if live else None,
    )


def write_manifest(directory: Path, *photos: PhotoMetadata) -> None:
    manifest = LegacyManifest(assets={photo.id: record_for(photo) for photo in photos})
    (directory / MANIFEST_NAME).write_text(manifest.model_dump_json())


def test_records_become_file_rows_and_a_live_photo_keeps_both(tmp_path: Path) -> None:
    write_manifest(tmp_path, make_photo("id-1", "IMG_1.HEIC", live_size=500))

    with library.library(tmp_path) as conn:
        result = migrate_manifest.import_manifest(conn, tmp_path)

        assert result.is_clean
        assert (result.records, result.files) == (1, 2)
        [asset] = library.backed_up(conn).values()
        assert [item.kind for item in asset.files] == [library.ORIGINAL, library.LIVE_VIDEO]
        assert asset.total_bytes == 1500


def test_an_asset_only_the_manifest_knows_is_restored_as_backup_only(tmp_path: Path) -> None:
    """The whole point of the record: on the drive, already freed from iCloud."""
    write_manifest(tmp_path, make_photo("id-freed", "IMG_1.HEIC"))

    with library.library(tmp_path) as conn:
        assert migrate_manifest.import_manifest(conn, tmp_path).restored == 1

        row = conn.execute("SELECT in_icloud, icloud_gone_at FROM assets").fetchone()
        assert (row["in_icloud"], row["icloud_gone_at"]) == (0, None)


def test_an_asset_a_pull_already_found_is_left_in_icloud(tmp_path: Path) -> None:
    """The import must not demote assets a fresher pull just confirmed."""
    photo = make_photo("id-1", "IMG_1.HEIC")
    write_manifest(tmp_path, photo)

    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [photo])
        result = migrate_manifest.import_manifest(conn, tmp_path)

        assert result.restored == 0
        assert conn.execute("SELECT in_icloud FROM assets").fetchone()["in_icloud"] == 1


def test_a_library_that_disagrees_with_the_manifest_is_reported(tmp_path: Path) -> None:
    """A verification that cannot fail is not verifying anything."""
    photo = make_photo("id-1", "IMG_1.HEIC")
    write_manifest(tmp_path, photo)

    with library.library(tmp_path) as conn:
        library.replace_assets(conn, [photo])
        library.record_files(
            conn,
            "id-1",
            [library.BackedUpFile(library.ORIGINAL, "2024/somewhere-else.HEIC", 1, "other")],
            recorded_as="downloaded",
        )

        result = migrate_manifest.import_manifest(conn, tmp_path)

        assert not result.is_clean
        assert "id-1" in result.discrepancies[0]


def test_importing_twice_changes_nothing(tmp_path: Path) -> None:
    """A re-run after an interruption has to be safe, not a source of duplicates."""
    write_manifest(tmp_path, make_photo("id-1", "IMG_1.HEIC", live_size=500))

    with library.library(tmp_path) as conn:
        first = migrate_manifest.import_manifest(conn, tmp_path)
        second = migrate_manifest.import_manifest(conn, tmp_path)

        assert second.is_clean
        assert (second.records, second.files) == (first.records, first.files)
        assert conn.execute("SELECT count(*) FROM assets").fetchone()[0] == 1


def test_a_drive_with_no_manifest_says_so(tmp_path: Path) -> None:
    with library.library(tmp_path) as conn, pytest.raises(FileNotFoundError):
        migrate_manifest.import_manifest(conn, tmp_path)
