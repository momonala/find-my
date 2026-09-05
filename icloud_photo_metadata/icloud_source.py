"""The only module that knows how pyicloud and Apple's CloudKit payloads work.

Quarantined on purpose. Reading photo metadata means leaning on pyicloud
internals -- the offset-paging call behind album iteration, raw CKRecord
fields, and the `mediaMetaDataEnc` EXIF blob pyicloud never decodes -- and a
patch release of a reverse-engineered library can move any of it. The rest of
the tool sees `PhotoMetadata` and nothing else.

Credentials and the session cookie jar are shared with the main Find My app, so
a session already trusted by `uv run mycloud devices` needs no second 2FA prompt.
"""

from __future__ import annotations

import base64
import os
import plistlib
import sys
import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv
from pyicloud import PyiCloudService
from pyicloud.services.photos_cloudkit.constants import DirectionEnum
from pyicloud.services.photos_cloudkit.constants import SmartAlbumEnum
from pyicloud.services.photos_cloudkit.mappers import decode_encrypted_text
from pyicloud.services.photos_cloudkit.mappers import record_field_value
from pyicloud.services.photos_cloudkit.materialize import build_xmp_metadata

from icloud_photo_metadata.models import GpsFix
from icloud_photo_metadata.models import PhotoMetadata
from icloud_photo_metadata.models import StoredResource

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"
# Mirrors src/tracking.py's SESSION_DIR rather than importing it, so this tool
# stays independent of the app it borrows credentials from.
_SESSION_DIR = _REPO_ROOT / ".icloud_session"

LIBRARY_ALBUM = SmartAlbumEnum.ALL_PHOTOS.value

# The main library listing omits Hidden and Recently Deleted, but Apple bills
# for both, so all three are needed to reconcile with reported storage usage.
SOURCE_ALBUMS = (
    LIBRARY_ALBUM,
    SmartAlbumEnum.HIDDEN.value,
    SmartAlbumEnum.RECENTLY_DELETED.value,
)

# One page or so between them, so a pull can afford to re-read them end to end
# and be certain of their membership.
SMALL_ALBUMS = tuple(name for name in SOURCE_ALBUMS if name != LIBRARY_ALBUM)

# Every request through pyicloud's session rewrites the cookie jar and the
# session JSON with a truncating open(), so two concurrent requests can
# interleave those writes and leave the session file half-written. Only the
# response bodies stream in parallel.
_SESSION_LOCK = threading.Lock()

_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0
_DOWNLOAD_CHUNK_BYTES = 1 << 20


class PageFetchError(Exception):
    """A page of photo metadata could not be fetched, retries exhausted."""


class DownloadError(Exception):
    """An asset's original file could not be fetched."""


@dataclass(frozen=True)
class PageTask:
    """One page of one album -- the unit of work a pull is split into."""

    album: Any
    album_name: str
    offset: int
    page_size: int


def authenticate() -> PyiCloudService:
    """Return an authenticated iCloud session, reusing the app's cookie jar.

    Raises:
        typer.Exit: If credentials are missing, 2FA is rejected, or Apple wants
            a code and there is no terminal to prompt on.
    """
    load_dotenv(_ENV_FILE)
    username = os.environ.get("ICLOUD_USERNAME", "")
    password = os.environ.get("ICLOUD_PASSWORD", "")
    if not username or not password:
        typer.echo(f"Set ICLOUD_USERNAME and ICLOUD_PASSWORD in {_ENV_FILE}.", err=True)
        raise typer.Exit(1)

    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    api = PyiCloudService(username, password, cookie_directory=str(_SESSION_DIR))

    if api.requires_2fa:
        if not sys.stdin.isatty():
            typer.echo("Apple wants a 2FA code but there is no terminal to prompt on.", err=True)
            raise typer.Exit(1)
        if not api.validate_2fa_code(typer.prompt("Enter the 2FA code sent to your trusted device")):
            typer.echo("2FA code was not accepted.", err=True)
            raise typer.Exit(1)
        if not api.is_trusted_session:
            api.trust_session()

    return api


def build_page_tasks(api: PyiCloudService, albums: Iterable[str] = SOURCE_ALBUMS) -> list[PageTask]:
    """Split the given albums into fetchable pages, newest page first.

    Offsets ascend and rank 0 is an album's newest asset, so the task list runs
    newest to oldest -- which is what lets a caller walk only the front of it.
    See `find_assets` for why the direction works out that way.
    """
    container = api.photos.albums
    tasks: list[PageTask] = []
    for album_name in albums:
        album = container.get(album_name)
        if album is None:
            typer.echo(f"Album {album_name!r} not found, skipping.", err=True)
            continue
        tasks.extend(
            PageTask(album=album, album_name=album_name, offset=offset, page_size=album.page_size)
            for offset in range(0, len(album), album.page_size)
        )
    return tasks


def album_length(api: PyiCloudService, album_name: str) -> int | None:
    """How many assets iCloud reports in an album, or None if it is gone.

    One count query rather than a crawl, which is what makes it affordable as a
    check on a pull that deliberately read only part of the album.
    """
    album = api.photos.albums.get(album_name)
    return None if album is None else len(album)


def fetch_page(task: PageTask) -> list[PhotoMetadata]:
    """Fetch and flatten one page of photo metadata.

    Pages are addressed by absolute offset rather than a cursor, so callers can
    fetch several concurrently over one session without stepping on each
    other's place in the sequence.

    Raises:
        PageFetchError: If the query still fails after `_MAX_RETRIES` attempts.
    """
    try:
        page = _read_page(task.album, task.offset, task.page_size)
    except Exception as error:
        raise PageFetchError(f"{task.album_name} offset {task.offset}: {error}") from error
    # Outside the retry: a record that will not decode is a bug to see, not a
    # transient failure to paper over with three more attempts.
    return [_to_metadata(photo, task.album_name) for photo in page]


def find_assets(
    api: PyiCloudService,
    wanted_ids: AbstractSet[str],
    albums: Iterable[str],
    *,
    on_page: Callable[[str, int], None] | None = None,
    resume_from: Mapping[str, int] | None = None,
) -> Iterator[Any]:
    """Yield the raw pyicloud assets for `wanted_ids`, stopping once all are found.

    `fetch_page` returns plain metadata; downloading needs the live asset
    object, whose signed URLs are short-lived and cannot be cached.

    Rank 0 is the album's *newest* asset and ASCENDING reads forward from
    there, so this walks newest to oldest. Do not "fix" it to DESCENDING,
    which reads backwards from a rank and would reach recent assets last.

    `album._get_photo(id)` is not the better answer it looks like: on a miss it
    iterates the whole album, so N stale ids cost N full crawls.

    Args:
        on_page: Given the album name and the offset just past each page read.
            Both a progress signal and the bookmark `resume_from` takes.
        resume_from: Per album, the offset to start at, so a caller that
            stopped mid-crawl does not repay for the walk it already did.
    """
    remaining = set(wanted_ids)
    for album_name in albums:
        album = api.photos.albums.get(album_name)
        if album is None:
            continue
        start = (resume_from or {}).get(album_name, 0)
        for offset in range(start, len(album), album.page_size):
            page = _read_page(album, offset, album.page_size)
            for asset in page:
                if asset.id not in remaining:
                    continue
                remaining.discard(asset.id)
                yield asset
                if not remaining:
                    return
            if on_page is not None:
                on_page(album_name, offset + album.page_size)


def _read_page(album: Any, offset: int, page_size: int) -> list[Any]:
    """One page of an album, fetched with the session to itself.

    Retried on any failure: Apple resets long-lived connections mid-crawl, and
    a page dropped that way costs one more request rather than the walk so far.
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with _SESSION_LOCK:
                return list(album._get_photos_at(offset, DirectionEnum.ASCENDING, page_size))
        except Exception:  # noqa: BLE001 -- reverse-engineered API, any failure is worth a retry
            if attempt == _MAX_RETRIES:
                raise
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable")


def download_original(
    api: PyiCloudService, asset: Any, destination: Path, *, version: str = "original"
) -> int:
    """Stream one of an asset's stored files to `destination`, returning bytes written.

    `.part` then rename, so an interrupted run never leaves a truncated file
    that looks complete to the next one.

    Args:
        version: Which stored resource to fetch. `original_video` is a Live
            Photo's motion clip; movie assets have no such resource.

    Raises:
        DownloadError: If the asset exposes no such resource, or the stream fails.
    """
    url = asset.download_url(version)
    if not url:
        raise DownloadError(f"{asset.filename}: no {version} resource to download")

    partial = destination.with_suffix(destination.suffix + ".part")
    written = 0
    try:
        with _SESSION_LOCK:
            response = api.photos.session.get(url, stream=True)
        with response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                    handle.write(chunk)
                    written += len(chunk)
    except Exception as error:
        partial.unlink(missing_ok=True)
        raise DownloadError(f"{asset.filename}: {error}") from error

    partial.replace(destination)
    return written


def _to_metadata(photo: Any, source_album: str) -> PhotoMetadata:
    """Build a `PhotoMetadata` from an already-fetched pair of CloudKit records.

    Issues no request and touches no image bytes: the page query returned both
    records whole and every field is decoded out of them.
    """
    master, asset = photo._master_record, photo._asset_record
    resources = {
        key: StoredResource(
            size_bytes=resource.size,
            content_type=resource.type,
            width=resource.width,
            height=resource.height,
            checksum=resource.checksum,
        )
        for key, resource in photo.resources.items()
    }
    xmp = build_xmp_metadata(asset)

    return PhotoMetadata(
        id=photo.id,
        master_id=photo.master_id,
        filename=photo.filename,
        source_album=source_album,
        item_type=photo.item_type,
        is_live_photo=photo.is_live_photo,
        asset_subtype_v2=record_field_value(asset, "assetSubtypeV2"),
        hdr_type=record_field_value(asset, "assetHDRType"),
        burst_flags=record_field_value(asset, "burstFlags"),
        data_class_type=record_field_value(master, "dataClassType"),
        resources=resources,
        total_storage_bytes=sum(r.size_bytes for r in resources.values() if r.size_bytes),
        asset_date=photo.asset_date,
        added_date=photo.added_date,
        original_creation_date=record_field_value(master, "originalCreationDate"),
        import_date=record_field_value(master, "importDate"),
        master_modified=record_field_value(master, "recordModificationDate"),
        asset_modified=record_field_value(asset, "recordModificationDate"),
        time_zone_name=decode_encrypted_text(asset, "timeZoneNameEnc"),
        time_zone_offset_seconds=record_field_value(asset, "timeZoneOffset"),
        duration_seconds=_duration_seconds(asset),
        video_frame_rate=record_field_value(master, "videoFrameRate"),
        orientation=xmp.orientation if xmp else None,
        original_orientation=record_field_value(master, "originalOrientation"),
        is_favorite=bool(record_field_value(asset, "isFavorite")),
        is_hidden=bool(record_field_value(asset, "isHidden")),
        rating=xmp.rating if xmp else None,
        title=xmp.title if xmp else None,
        description=xmp.description if xmp else None,
        keywords=xmp.keywords if xmp else None,
        people=_json_safe(record_field_value(asset, "people") or []),
        view_count=record_field_value(asset, "viewCount"),
        play_count=record_field_value(asset, "playCount"),
        share_count=record_field_value(asset, "shareCount"),
        imported_by_bundle_id=decode_encrypted_text(master, "importedByBundleIdentifierEnc"),
        imported_by_display_name=decode_encrypted_text(master, "importedByDisplayNameEnc"),
        location=_gps_fix(xmp),
        exif=_decode_exif(master),
    )


def _duration_seconds(asset: Any) -> float | None:
    """Apple reports duration in milliseconds."""
    raw = record_field_value(asset, "duration")
    return raw / 1000 if raw else None


def _gps_fix(xmp: Any) -> GpsFix | None:
    """Build a location from decoded XMP, if the asset recorded one."""
    if xmp is None or xmp.gps_latitude is None or xmp.gps_longitude is None:
        return None
    return GpsFix(
        latitude=xmp.gps_latitude,
        longitude=xmp.gps_longitude,
        altitude=xmp.gps_altitude,
        speed=xmp.gps_speed,
        timestamp=xmp.gps_timestamp,
    )


def _decode_exif(master: Any) -> dict[str, Any] | None:
    """Decode `mediaMetaDataEnc`, the binary-plist EXIF/TIFF/GPS dump.

    pyicloud never touches this field, yet it is the richest on the record:
    camera make and model, lens, exposure, ISO, GPS heading.
    """
    value = record_field_value(master, "mediaMetaDataEnc")
    if not value:
        return None
    raw = value if isinstance(value, bytes) else base64.b64decode(value)
    try:
        parsed = plistlib.loads(raw)
    except plistlib.InvalidFileException:
        return None
    return _json_safe(parsed) if isinstance(parsed, dict) else None


def _json_safe(value: Any) -> Any:
    """Coerce a decoded plist value into JSON-serializable types.

    Raises:
        TypeError: On a type never yet seen out of a Photos plist. A loud
            failure naming the type beats a silently stringified field.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    raise TypeError(f"unexpected plist value type {type(value).__name__}")
