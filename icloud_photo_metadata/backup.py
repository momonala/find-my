"""Back up the iCloud media library to a directory on disk.

Photos, videos and GIFs are one kind of thing here: an asset with original bytes
worth keeping.

Indexing is a library lookup by asset id -- no guessing, no probing, no content
matching. That holds because the backup directory contains only files this tool
downloaded, one per asset, under a name carrying that asset's id.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Literal
from typing import NamedTuple

from tqdm import tqdm

from icloud_photo_metadata import library
from icloud_photo_metadata.drive import scan
from icloud_photo_metadata.drive import sha256
from icloud_photo_metadata.icloud_source import DownloadError
from icloud_photo_metadata.icloud_source import authenticate
from icloud_photo_metadata.icloud_source import download_original
from icloud_photo_metadata.icloud_source import find_assets
from icloud_photo_metadata.library import BackedUpAsset
from icloud_photo_metadata.library import BackedUpFile
from icloud_photo_metadata.models import PhotoMetadata
from icloud_photo_metadata.pull_metadata import PullResult
from icloud_photo_metadata.pull_metadata import refresh_incremental

DEFAULT_DIR = Path("/Volumes/TOSHIBA_1TB/pictures/icloud-backup")

EXCLUDED_ALBUMS = frozenset({"Hidden", "Recently Deleted"})

# A run interrupted between writing a file and recording it leaves the first
# kind looking like the second.
RecordSource = Literal["downloaded", "found on the drive"]

# A run this long is the signature of a dead session, not of bad assets.
_MAX_CONSECUTIVE_FAILURES = 10

# Without a cap, an asset failing for its own reasons stalls every pass forever.
_MAX_ASSET_ATTEMPTS = 2

# Any progress resets this, so a long sync survives any number of expiries.
MAX_FRUITLESS_REFRESHES = 3


def eligible(photos: Sequence[PhotoMetadata]) -> list[PhotoMetadata]:
    """The assets this drive should hold a copy of."""
    return [photo for photo in photos if photo.source_album not in EXCLUDED_ALBUMS]


@dataclass(frozen=True)
class BackupIndex:
    """Which eligible assets have their original bytes on the drive."""

    recorded: list[str]
    missing: list[PhotoMetadata]
    # Always empty in a healthy backup: a file no record claims is an anomaly.
    stray: list[Path]


def build_index(
    photos: Sequence[PhotoMetadata],
    files: dict[Path, int],
    backed_up: dict[str, BackedUpAsset],
    directory: Path,
) -> BackupIndex:
    """Work out which assets the drive already holds.

    An asset whose file has vanished or changed size counts as missing, so a
    download repairs the backup; `verify` reports it as damage instead.
    """
    recorded: list[str] = []
    missing: list[PhotoMetadata] = []
    for photo in photos:
        asset = backed_up.get(photo.id)
        intact = asset is not None and all(
            files.get(directory / item.relative_path) == item.size_bytes for item in asset.files
        )
        if intact:
            recorded.append(photo.id)
        else:
            missing.append(photo)

    claimed = {path for asset in backed_up.values() for path in asset.paths(directory)}
    return BackupIndex(recorded=recorded, missing=missing, stray=sorted(set(files) - claimed))


class _Wanted(NamedTuple):
    """One file to fetch: what the library calls it, what Apple calls it, where it goes."""

    kind: str
    version: str
    destination: Path
    expected_bytes: int


@dataclass(frozen=True)
class Download:
    """An iCloud asset with no intact copy on the drive, and where it will land."""

    photo: PhotoMetadata
    destination: Path
    live_destination: Path | None

    @property
    def expected_bytes(self) -> int:
        return (self.photo.original_size_bytes or 0) + (self.photo.live_video_size_bytes or 0)

    @property
    def destinations(self) -> list[Path]:
        return [self.destination] + ([self.live_destination] if self.live_destination else [])

    def wanted(self) -> list[_Wanted]:
        """Every file this download owes, in the order they are recorded."""
        files = [_Wanted(library.ORIGINAL, "original", self.destination, self.photo.original_size_bytes or 0)]
        if self.live_destination is not None:
            files.append(
                _Wanted(
                    library.LIVE_VIDEO,
                    "original_video",
                    self.live_destination,
                    self.photo.live_video_size_bytes or 0,
                )
            )
        return files


@dataclass(frozen=True)
class SyncPlan:
    """Everything a sync would do. Empty means the drive already matches iCloud."""

    downloads: list[Download] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.downloads

    @property
    def download_bytes(self) -> int:
        return sum(download.expected_bytes for download in self.downloads)


def backup_filename(photo: PhotoMetadata) -> Path:
    """The drive-relative path a downloaded asset takes.

    The id fragment is unconditional because iPhone recycles `IMG_1234`
    numbering after a reset, so two live assets can claim one name. Carrying the
    id makes every name unique with no collision search.
    """
    name = Path(photo.filename)
    return Path(str(photo.asset_date.year)) / f"{name.stem}~{photo.id[:8]}{name.suffix}"


def live_filename(photo: PhotoMetadata) -> Path:
    """A Live Photo's motion clip, which is always a .MOV beside the still."""
    return backup_filename(photo).with_suffix(".MOV")


def build_plan(index: BackupIndex, directory: Path) -> SyncPlan:
    """Turn the index into the work a sync would do.

    Desired state rather than steps, so applying it twice is a no-op.

    Raises:
        ValueError: If two files want one path, which would mean one silently
            overwriting the other.
    """
    downloads = [
        Download(
            photo=photo,
            destination=directory / backup_filename(photo),
            live_destination=directory / live_filename(photo) if photo.live_video else None,
        )
        for photo in index.missing
    ]
    wanted = [path for download in downloads for path in download.destinations]
    if len(set(wanted)) != len(wanted):
        raise ValueError("two files resolved to one path; the id fragment is not unique")
    return SyncPlan(downloads=downloads)


class MetadataPullFailed(Exception):
    """The metadata pull lost pages, so the library was left as it was.

    Planning against a partial view could miss whole assets, and a download
    reporting success against it would claim a backup it never made.
    """

    def __init__(self, result: PullResult) -> None:
        super().__init__(f"{result.failed_pages} metadata pages failed after retries.")
        self.result = result


@dataclass(frozen=True)
class Survey:
    """What one look at iCloud and the drive found."""

    plan: SyncPlan
    index: BackupIndex
    api: Any
    # Not just the eligible ones: the comparison needs Hidden and Recently
    # Deleted to tell a freed asset from a hidden one.
    in_icloud: list[PhotoMetadata]
    pulled: PullResult
    files_on_drive: int


def survey(
    conn: sqlite3.Connection,
    directory: Path,
    *,
    api: Any | None = None,
    quiet: bool = False,
) -> Survey:
    """Refresh the metadata, then work out what the drive is missing.

    Shared by the CLI's plan and download and by the web's refresh, so the
    three cannot disagree. The refresh is not optional: a plan built from
    whatever the library last held could report the drive matches iCloud
    having never asked iCloud anything.

    Raises:
        MetadataPullFailed: If the pull lost pages. Nothing was written.
    """
    if api is None:
        api = authenticate()
    pulled = refresh_incremental(conn, api, quiet=quiet)
    if not pulled.is_complete:
        raise MetadataPullFailed(pulled)

    # A freed asset cannot be downloaded, so counting it missing would put it
    # in every plan forever.
    in_icloud = library.load_assets(conn, in_icloud_only=True)
    photos = eligible(in_icloud)
    files = scan(directory)

    index = build_index(photos, files, library.backed_up(conn), directory)
    return Survey(
        plan=build_plan(index, directory),
        index=index,
        api=api,
        in_icloud=in_icloud,
        pulled=pulled,
        files_on_drive=len(files),
    )


@dataclass
class Outcome:
    """What applying a plan actually managed to do."""

    downloaded: int = 0
    failed: int = 0
    aborted: bool = False
    sessions_reopened: int = 0
    # Still owed when the sync stopped. Already counted in `failed`.
    remaining: int = 0
    # Filenames no album turned up. Empty on an aborted run, which cannot tell
    # a missing asset from an unreached one.
    unfetched: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _PassResult:
    # A pass stalls on a run of download failures or on the crawl losing the
    # session. Both read the same way: re-authenticate and resume.
    stalled: bool
    downloaded: int


@dataclass(frozen=True)
class _Fetched:
    """One asset's files, on the drive and waiting to be recorded."""

    files: tuple[BackedUpFile, ...]
    sources: tuple[RecordSource, ...]

    @property
    def recorded_as(self) -> RecordSource:
        if set(self.sources) == {"found on the drive"}:
            return "found on the drive"
        return "downloaded"


@dataclass
class _Sync:
    """What survives from one pass of `apply_plan` to the next.

    An asset leaves `outstanding` when its files are recorded, or when it has
    failed often enough to be called failed. State that resets with each pass
    stays local to `_run_pass`.
    """

    directory: Path
    conn: sqlite3.Connection
    progress: tqdm
    outstanding: dict[str, Download]
    outcome: Outcome = field(default_factory=Outcome)
    failures_by_asset: dict[str, int] = field(default_factory=dict)
    # A pass that re-opens the session resumes from these rather than walking
    # the whole library again.
    next_offset_by_album: dict[str, int] = field(default_factory=dict)
    pages_crawled: int = 0

    def report(self) -> None:
        """Put the counters on the bar.

        The count itself moves only when a download is recorded, and the crawl
        can page past thousands of backed-up assets first -- minutes of a bar
        sitting at zero.
        """
        self.progress.set_postfix(pages=self.pages_crawled, failed=self.outcome.failed)


def apply_plan(
    plan: SyncPlan,
    directory: Path,
    conn: sqlite3.Connection,
    *,
    api: Any | None = None,
) -> Outcome:
    """Execute the plan, recording each asset in the library as it lands.

    A long run of failures means the session expired rather than the library
    being broken, so the sync re-opens it and resumes with what it still owes,
    giving up only once re-authenticating buys no downloads at all.

    Writes nothing but the progress bar; what happened is on the `Outcome`.

    Args:
        api: An authenticated session to reuse. Left out, one is opened here.
    """
    if not plan.downloads:
        return Outcome()

    if api is None:
        api = authenticate()
    sync = _Sync(
        directory=directory,
        conn=conn,
        progress=tqdm(total=len(plan.downloads), unit="file", desc="Downloading", disable=None),
        outstanding={download.photo.id: download for download in plan.downloads},
    )

    fruitless_refreshes = 0
    try:
        while sync.outstanding:
            resumed = bool(sync.next_offset_by_album)
            result = _run_pass(sync, api)
            if not result.stalled:
                # A resumed pass skipped the pages an earlier one had read, so
                # anything still outstanding might be back there rather than
                # gone. One full sweep settles it before calling it missing.
                if resumed and sync.outstanding:
                    sync.next_offset_by_album.clear()
                    continue
                break
            fruitless_refreshes = 0 if result.downloaded else fruitless_refreshes + 1
            if fruitless_refreshes >= MAX_FRUITLESS_REFRESHES:
                sync.outcome.aborted = True
                break
            sync.outcome.sessions_reopened += 1
            tqdm.write(f"  re-opening the session and resuming with {len(sync.outstanding):,} to go.")
            api = authenticate()
    finally:
        sync.progress.close()

    sync.outcome.remaining = len(sync.outstanding)
    sync.outcome.failed += len(sync.outstanding)
    if not sync.outcome.aborted:
        sync.outcome.unfetched = [item.photo.filename for item in sync.outstanding.values()]
    return sync.outcome


def _run_pass(sync: _Sync, api: Any) -> _PassResult:
    """One crawl of the albums over one session, downloading each asset it finds.

    Serial throughout: Apple serves the crawl and the downloads over one
    session, so concurrency buys little while making a dropped connection
    everyone's problem.
    """
    downloaded_before = sync.outcome.downloaded
    consecutive_failures = 0
    albums = {download.photo.source_album for download in sync.outstanding.values()}

    def on_page(album_name: str, next_offset: int) -> None:
        sync.next_offset_by_album[album_name] = next_offset
        sync.pages_crawled += 1
        sync.report()

    def ended(*, stalled: bool) -> _PassResult:
        return _PassResult(stalled=stalled, downloaded=sync.outcome.downloaded - downloaded_before)

    assets = iter(
        find_assets(
            api,
            set(sync.outstanding),
            albums,
            on_page=on_page,
            resume_from=dict(sync.next_offset_by_album),
        )
    )
    while True:
        try:
            asset = next(assets)
        except StopIteration:
            return ended(stalled=False)
        except Exception as error:  # noqa: BLE001 -- a dead session surfaces as whatever requests raises
            tqdm.write(f"  ! the album crawl failed: {error}")
            return ended(stalled=True)
        if _fetch_and_record(sync, sync.outstanding[asset.id], api, asset):
            consecutive_failures = 0
        else:
            consecutive_failures += 1
        if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            return ended(stalled=True)


def _fetch_and_record(sync: _Sync, download: Download, api: Any, asset: Any) -> bool:
    """Fetch one asset and record it in the library, or count it failed.

    An asset that has failed `_MAX_ASSET_ATTEMPTS` times stops being
    outstanding: a fresh session will not change why it is failing.

    Returns:
        Whether the asset landed.
    """
    asset_id = download.photo.id
    try:
        fetched = _fetch_asset(api, asset, download, sync.directory)
    except DownloadError as error:
        tqdm.write(f"  ! {error}")
        sync.failures_by_asset[asset_id] = sync.failures_by_asset.get(asset_id, 0) + 1
        if sync.failures_by_asset[asset_id] >= _MAX_ASSET_ATTEMPTS:
            del sync.outstanding[asset_id]
            sync.outcome.failed += 1
            sync.progress.update(1)
        sync.report()
        return False

    _record(sync.conn, download, fetched)
    del sync.outstanding[asset_id]
    sync.outcome.downloaded += 1
    sync.progress.update(1)
    sync.report()
    return True


def _fetch_asset(api: Any, asset: Any, download: Download, directory: Path) -> _Fetched:
    """Fetch one asset's files onto the drive.

    Raises:
        DownloadError: If any of its files could not be landed intact.
    """
    files: list[BackedUpFile] = []
    sources: list[RecordSource] = []
    for item in download.wanted():
        digest, source = _fetch(api, asset, item.version, item.destination, item.expected_bytes)
        relative_path = str(item.destination.relative_to(directory))
        files.append(BackedUpFile(item.kind, relative_path, item.expected_bytes, digest))
        sources.append(source)
    return _Fetched(files=tuple(files), sources=tuple(sources))


def _record(conn: sqlite3.Connection, download: Download, fetched: _Fetched) -> None:
    """Write one landed asset into the library."""
    for path in download.destinations:
        _stamp_capture_date(path, download.photo)
    library.record_files(conn, download.photo.id, fetched.files, recorded_as=fetched.recorded_as)


def _fetch(api: Any, asset: Any, version: str, destination: Path, expected: int) -> tuple[str, RecordSource]:
    """Ensure `destination` holds `expected` bytes of `asset`, and hash them.

    Keeping a file already there at the right size is what lets a run
    interrupted between writing and recording cost bookkeeping, not bytes.

    Returns:
        The file's hash, and how it got there.

    Raises:
        DownloadError: If the stream failed, or the file on the drive is the
            wrong size -- damage to look at, not something to overwrite.
    """
    if destination.exists():
        found = destination.stat().st_size
        if found != expected:
            raise DownloadError(f"{destination.name}: on the drive at {found} bytes, expected {expected}")
        return sha256(destination), "found on the drive"

    destination.parent.mkdir(parents=True, exist_ok=True)
    written = download_original(api, asset, destination, version=version)
    if written != expected:
        destination.unlink(missing_ok=True)
        raise DownloadError(f"{destination.name}: got {written} bytes, expected {expected}")
    return sha256(destination), "downloaded"


def _stamp_capture_date(path: Path, photo: PhotoMetadata) -> None:
    """Set the file's mtime to the capture date so the directory sorts usefully."""
    taken = photo.asset_date.timestamp()
    os.utime(path, (taken, taken))
