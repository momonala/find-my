"""Read the media files sitting on the backup drive.

Everything that touches the filesystem is here; the rest of the tool works from
the `path -> size` mapping `scan` returns. Size is all the drive is asked for,
because identifying a file is a library lookup, not an inference from bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from icloud_photo_metadata.library import DUMP_NAME
from icloud_photo_metadata.library import LIBRARY_NAME

_HASH_CHUNK_BYTES = 1 << 20

# Everything on the drive that is not media: macOS resource-fork stubs, Finder
# droppings, the library and its journal, a partial download, an edit sidecar.
_IGNORED_PREFIXES = ("._",)
_IGNORED_NAMES = frozenset({".DS_Store", LIBRARY_NAME, LIBRARY_NAME + "-journal", DUMP_NAME})
_IGNORED_SUFFIXES = frozenset({".aae", ".part"})


def media_files(directory: Path) -> list[Path]:
    """Every media file under the directory, at any depth."""
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and not path.name.startswith(_IGNORED_PREFIXES)
        and path.name not in _IGNORED_NAMES
        and path.suffix.lower() not in _IGNORED_SUFFIXES
    )


def scan(directory: Path) -> dict[Path, int]:
    """Map every media file on the drive to its size in bytes."""
    return {path: path.stat().st_size for path in media_files(directory)}


def sha256(path: Path) -> str:
    """Hash a file's bytes, streaming so a large video never lands in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
