"""Browser-renderable JPEGs made from the originals on the drive.

76% of the library is HEIC, which only Safari renders, and an original is a
4032x3024 file on a USB disk -- so a grid needs something else to show. Making
them on request keeps the drive holding nothing but originals: the cache lives
under `data/`, and deleting it costs one slow page.

Each conversion is ~0.3s, so the cache is what makes the gallery usable rather
than an optimisation on top of it.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pillow_heif
from PIL import Image
from PIL import ImageOps
from PIL import UnidentifiedImageError

from src.core.paths import DATA_DIR

# Registers HEIF/HEIC with Pillow's open(). Without it every iPhone still in
# the library raises UnidentifiedImageError.
pillow_heif.register_heif_opener()

CACHE_DIR = DATA_DIR / "photo-thumbs"

SIZES = {"grid": 400, "detail": 1600}

_JPEG_QUALITY = 82


class UnrenderableAsset(RuntimeError):
    """The original is not an image Pillow can decode -- a video, or a broken file."""


def thumbnail(original: Path, size: str) -> Path:
    """The cached JPEG for `original` at `size`, building it if it is not there.

    Raises:
        KeyError: If `size` names no known dimension.
        FileNotFoundError: If the original is not on the drive.
        UnrenderableAsset: If Pillow cannot decode it.
    """
    edge = SIZES[size]
    cached = _cache_path(original, size)
    if cached.exists():
        return cached
    if not original.exists():
        raise FileNotFoundError(f"No original at {original}.")

    cached.parent.mkdir(parents=True, exist_ok=True)
    handle, partial_name = tempfile.mkstemp(dir=cached.parent, suffix=".part")
    os.close(handle)
    partial = Path(partial_name)
    try:
        with Image.open(original) as image:
            # iPhone writes the sensor's orientation to EXIF rather than
            # rotating the pixels, so without this a third of the grid is sideways.
            upright = ImageOps.exif_transpose(image)
            upright.thumbnail((edge, edge))
            upright.convert("RGB").save(partial, "JPEG", quality=_JPEG_QUALITY)
        partial.replace(cached)
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise UnrenderableAsset(f"{original.name}: {error}") from error
    finally:
        partial.unlink(missing_ok=True)
    return cached


def _cache_path(original: Path, size: str) -> Path:
    """Where one thumbnail lives: keyed by the original's path, not its name.

    Two assets can share a filename, so the path is what makes the key unique;
    hashing it keeps the cache flat and free of the drive's own directories.
    """
    digest = hashlib.sha256(str(original).encode()).hexdigest()
    return CACHE_DIR / size / f"{digest}.jpg"
