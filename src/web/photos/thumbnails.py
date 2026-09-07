"""Browser-renderable JPEGs made from the originals on the drive.

76% of the library is HEIC, which only Safari renders, and an original is a
4032x3024 file on a USB disk -- so a grid needs something else to show. Making
them on request keeps the drive holding nothing but originals: the cache lives
under `data/`, and deleting it costs one slow page.

Each conversion is ~0.3s, so the cache is what makes the gallery usable rather
than an optimisation on top of it.

A movie has no still on the drive, so its tile is a frame taken with whatever
`ffmpeg` is on PATH -- 0.12s, cached the same way. There is no Python video
dependency and nothing to install: without ffmpeg a movie simply has no
thumbnail, which is what the gallery showed for all of them before.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
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


# The movie containers, as opposed to the animated stills Apple also files as
# `item_type = movie` (GIF, WEBP, AVIF), which Pillow renders like any photo.
# Also what the video route will hand a browser to play.
VIDEO_SUFFIXES = frozenset({".mov", ".mp4", ".m4v"})

# Long enough for a frame off a USB disk, short enough that a file ffmpeg
# cannot make sense of does not hold a request thread.
_FRAME_TIMEOUT_SECONDS = 30


class UnrenderableAsset(RuntimeError):
    """Nothing on the drive can be turned into a picture of this asset."""


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
    # A frame is a full-size JPEG that then goes through the same resize as any
    # other original, so a movie's tile and a photo's are made the same way.
    frame = _poster_frame(original) if original.suffix.lower() in VIDEO_SUFFIXES else None
    try:
        with Image.open(frame or original) as image:
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
        if frame is not None:
            frame.unlink(missing_ok=True)
    return cached


def _poster_frame(video: Path) -> Path:
    """A still from the first second of `video`, as a temporary JPEG.

    ffmpeg is looked up per call rather than at import, so installing it does
    not need the server restarted.

    Raises:
        UnrenderableAsset: If there is no ffmpeg, or it cannot read the file.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise UnrenderableAsset(f"{video.name}: no ffmpeg on PATH to take a frame from")

    handle, name = tempfile.mkstemp(suffix=".jpg")
    os.close(handle)
    frame = Path(name)
    try:
        # `-f image2` because the destination is a temp file ffmpeg cannot read
        # a format from. Rotation is applied from the container's matrix, which
        # is the only reason a phone's video comes out upright.
        result = subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-loglevel",
                "error",
                "-ss",
                "0",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-f",
                "image2",
                "-y",
                str(frame),
            ],
            capture_output=True,
            timeout=_FRAME_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        frame.unlink(missing_ok=True)
        raise UnrenderableAsset(f"{video.name}: {error}") from error

    if result.returncode != 0 or not frame.stat().st_size:
        detail = result.stderr.decode(errors="replace").strip() or "no frame came back"
        frame.unlink(missing_ok=True)
        raise UnrenderableAsset(f"{video.name}: {detail}")
    return frame


def _cache_path(original: Path, size: str) -> Path:
    """Where one thumbnail lives: keyed by the original's path, not its name.

    Two assets can share a filename, so the path is what makes the key unique;
    hashing it keeps the cache flat and free of the drive's own directories.
    """
    digest = hashlib.sha256(str(original).encode()).hexdigest()
    return CACHE_DIR / size / f"{digest}.jpg"
