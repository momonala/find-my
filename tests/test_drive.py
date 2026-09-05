"""Scanning the drive: what counts as media, and what is the drive's own litter."""

import hashlib
from pathlib import Path

from icloud_photo_metadata import drive
from icloud_photo_metadata.drive import media_files
from icloud_photo_metadata.drive import scan
from icloud_photo_metadata.drive import sha256


def test_drive_clutter_is_not_mistaken_for_media(tmp_path: Path) -> None:
    """macOS and this tool both leave files on the drive that are not media."""
    clutter = (
        "._IMG_1.MOV",
        ".DS_Store",
        "IMG_1.aae",
        "IMG_2.MOV.part",
        drive.LIBRARY_NAME,
        drive.LIBRARY_NAME + "-journal",
    )
    for name in ("IMG_1.MOV", *clutter):
        (tmp_path / name).write_bytes(b"x")

    assert [path.name for path in media_files(tmp_path)] == ["IMG_1.MOV"]


def test_scan_reaches_into_the_year_directories(tmp_path: Path) -> None:
    """Files live under <year>/, so a flat listing would find nothing."""
    (tmp_path / "2024").mkdir()
    (tmp_path / "2024" / "IMG_1~abcd1234.HEIC").write_bytes(b"12345")

    assert scan(tmp_path) == {tmp_path / "2024" / "IMG_1~abcd1234.HEIC": 5}


def test_sha256_streams_the_whole_file(tmp_path: Path) -> None:
    """The hash is what catches a file that rots without changing size."""
    photo = tmp_path / "IMG_1.HEIC"
    photo.write_bytes(b"a" * (2 << 20))

    assert sha256(photo) == hashlib.sha256(b"a" * (2 << 20)).hexdigest()
