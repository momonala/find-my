"""The backup pipeline: which assets are eligible, indexed, planned, applied.

The pure stages run on plain values, with no database in sight. `apply_plan`
runs against a fake iCloud but a real filesystem and a real library, because
what is worth pinning there is all about what ends up on disk.
"""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from icloud_photo_metadata import backup
from icloud_photo_metadata import cli
from icloud_photo_metadata import library
from icloud_photo_metadata.backup import Outcome
from icloud_photo_metadata.backup import apply_plan
from icloud_photo_metadata.backup import backup_filename
from icloud_photo_metadata.backup import build_index
from icloud_photo_metadata.backup import build_plan
from icloud_photo_metadata.backup import eligible
from icloud_photo_metadata.cli import app
from icloud_photo_metadata.drive import scan
from icloud_photo_metadata.icloud_source import DownloadError
from icloud_photo_metadata.library import BackedUpAsset
from icloud_photo_metadata.library import BackedUpFile
from icloud_photo_metadata.models import PhotoMetadata
from icloud_photo_metadata.pull_metadata import PullResult
from tests.icloud_factories import make_photo


@dataclass
class FakeAsset:
    """Stands in for the live pyicloud asset a download needs."""

    id: str
    filename: str


@pytest.fixture
def icloud(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], bytes]:
    """Fake iCloud downloads. Map `(asset id, version)` to the bytes it yields."""
    payloads: dict[tuple[str, str], bytes] = {}

    def find_assets(
        api: object,
        wanted_ids: set[str],
        albums: set[str],
        *,
        on_page: object = None,
        resume_from: object = None,
    ) -> list[FakeAsset]:
        found = {asset_id for asset_id, _ in payloads}
        return [FakeAsset(id=i, filename=f"{i}.HEIC") for i in sorted(wanted_ids) if i in found]

    def download_original(api: object, asset: FakeAsset, destination: Path, *, version: str) -> int:
        body = payloads[(asset.id, version)]
        destination.write_bytes(body)
        return len(body)

    monkeypatch.setattr(backup, "authenticate", lambda: object())
    monkeypatch.setattr(backup, "find_assets", find_assets)
    monkeypatch.setattr(backup, "download_original", download_original)
    return payloads


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A library on the same temp directory the backup writes into."""
    with library.library(tmp_path) as connection:
        yield connection


def holding(*files: tuple[str, str, int]) -> dict[str, BackedUpAsset]:
    """The `asset id -> files` mapping `build_index` reads, as plain values.

    Each entry is `(asset id, drive-relative path, size)`; naming one asset
    twice gives it a Live Photo's two files.
    """
    grouped: dict[str, list[BackedUpFile]] = {}
    for asset_id, relative_path, size in files:
        kind = library.ORIGINAL if asset_id not in grouped else library.LIVE_VIDEO
        grouped.setdefault(asset_id, []).append(
            BackedUpFile(kind, relative_path, size, f"hash-of-{relative_path}")
        )
    return {
        asset_id: BackedUpAsset(asset_id=asset_id, files=tuple(items)) for asset_id, items in grouped.items()
    }


def _plan_for(
    photos: list[PhotoMetadata], directory: Path, backed_up: dict[str, BackedUpAsset] | None = None
) -> tuple[backup.BackupIndex, backup.SyncPlan]:
    index = build_index(photos, scan(directory), backed_up or {}, directory)
    return index, build_plan(index, directory)


def seed(directory: Path, *photos: PhotoMetadata, freed: tuple[PhotoMetadata, ...] = ()) -> None:
    """Give the library at `directory` a view of iCloud for a command to read."""
    with library.library(directory) as connection:
        library.replace_assets(connection, list(photos))
        library.restore_assets(connection, list(freed))


def record(directory: Path, asset_id: str, *paths: Path) -> None:
    """Note that the drive holds `paths` for `asset_id`, hashing them for real."""
    with library.library(directory) as connection:
        library.record_files(
            connection,
            asset_id,
            [
                BackedUpFile(
                    library.ORIGINAL if index == 0 else library.LIVE_VIDEO,
                    str(path.relative_to(directory)),
                    path.stat().st_size,
                    backup.sha256(path),
                )
                for index, path in enumerate(paths)
            ],
            recorded_as="downloaded",
        )


# --- selection and naming ---------------------------------------------------


def test_hidden_and_recently_deleted_are_never_downloaded() -> None:
    keep = make_photo("id-1", "IMG_1.HEIC")
    hidden = make_photo("id-2", "IMG_2.HEIC", album="Hidden")
    binned = make_photo("id-3", "IMG_3.MOV", album="Recently Deleted")

    assert [photo.id for photo in eligible([keep, hidden, binned])] == ["id-1"]


def test_every_media_type_is_eligible() -> None:
    """Photos, videos and GIFs are one kind of thing: bytes worth keeping.

    Filtering on `item_type` silently excludes the library's GIFs, WEBPs and
    AVIFs.
    """
    assets = [
        make_photo("id-1", "IMG_1.HEIC"),
        make_photo("id-2", "IMG_2.MOV", item_type="movie"),
        make_photo("id-3", "IMG_3.GIF"),
        make_photo("id-4", "IMG_4.AVIF"),
    ]

    assert len(eligible(assets)) == 4


def test_a_download_is_filed_under_its_capture_year() -> None:
    photo = make_photo("abcd1234efgh", "IMG_1.HEIC")

    assert backup_filename(photo) == Path("2024/IMG_1~abcd1234.HEIC")


def test_recycled_filenames_get_distinct_destinations(tmp_path: Path) -> None:
    """iPhone reuses IMG_1234 after a reset: 1,324 collisions in this library."""
    photos = [make_photo("aaaaaaaa11", "IMG_1.HEIC"), make_photo("bbbbbbbb22", "IMG_1.HEIC")]

    _, plan = _plan_for(photos, tmp_path)

    assert {d.destination.name for d in plan.downloads} == {"IMG_1~aaaaaaaa.HEIC", "IMG_1~bbbbbbbb.HEIC"}


def test_two_files_resolving_to_one_path_is_an_error(tmp_path: Path) -> None:
    """Silently overwriting one backup with another is the worst possible bug."""
    twins = [make_photo("dupe1234", "IMG_1.HEIC"), make_photo("dupe1234", "IMG_1.HEIC")]

    with pytest.raises(ValueError, match="id fragment is not unique"):
        _plan_for(twins, tmp_path)


def test_a_live_photos_motion_clip_shares_the_stills_path(tmp_path: Path) -> None:
    photo = make_photo("abcd1234", "IMG_1.HEIC", size=100, live_size=400)

    _, plan = _plan_for([photo], tmp_path)
    [download] = plan.downloads

    assert download.destination == tmp_path / "2024/IMG_1~abcd1234.HEIC"
    assert download.live_destination == tmp_path / "2024/IMG_1~abcd1234.MOV"
    assert download.expected_bytes == 500


def test_a_movie_asset_gets_no_motion_clip(tmp_path: Path) -> None:
    """Only images carry `original_video`; asking a movie for one would 404."""
    _, plan = _plan_for([make_photo("id-1", "IMG_1.MOV", item_type="movie")], tmp_path)

    assert plan.downloads[0].live_destination is None


# --- indexing ---------------------------------------------------------------


def test_the_library_alone_decides_what_is_backed_up(tmp_path: Path) -> None:
    """No probing, no content matching: an id lookup and a size check."""
    photo = make_photo("id-1", "IMG_1.HEIC", size=4)
    (tmp_path / "2024").mkdir()
    (tmp_path / "2024" / "IMG_1~id-1.HEIC").write_bytes(b"1234")

    index, plan = _plan_for([photo], tmp_path, holding(("id-1", "2024/IMG_1~id-1.HEIC", 4)))

    assert index.recorded == ["id-1"]
    assert plan.is_empty


@pytest.mark.parametrize("damage", ["delete", "resize"], ids=["file_gone", "size_changed"])
def test_a_broken_record_is_replanned_as_a_download(tmp_path: Path, damage: str) -> None:
    photo = make_photo("id-1", "IMG_1.HEIC", size=4)
    (tmp_path / "2024").mkdir()
    recorded = tmp_path / "2024" / "IMG_1~id-1.HEIC"
    recorded.write_bytes(b"1234")

    if damage == "delete":
        recorded.unlink()
    else:
        recorded.write_bytes(b"12345678")

    index, plan = _plan_for([photo], tmp_path, holding(("id-1", "2024/IMG_1~id-1.HEIC", 4)))

    assert index.recorded == []
    assert [d.photo.id for d in plan.downloads] == ["id-1"]


def test_a_live_photo_missing_its_motion_clip_is_not_backed_up(tmp_path: Path) -> None:
    """Half a Live Photo is a broken record, not a complete one."""
    photo = make_photo("id-1", "IMG_1.HEIC", size=4, live_size=6)
    (tmp_path / "2024").mkdir()
    (tmp_path / "2024" / "IMG_1~id-1.HEIC").write_bytes(b"1234")

    index, _ = _plan_for(
        [photo],
        tmp_path,
        holding(("id-1", "2024/IMG_1~id-1.HEIC", 4), ("id-1", "2024/IMG_1~id-1.MOV", 6)),
    )

    assert index.recorded == []


def test_a_file_no_record_claims_is_reported_as_stray(tmp_path: Path) -> None:
    """This directory holds only what a sync put here, so anything else is a leftover."""
    (tmp_path / "2024").mkdir()
    (tmp_path / "2024" / "who-put-this-here.HEIC").write_bytes(b"x")

    index, _ = _plan_for([], tmp_path)

    assert index.stray == [tmp_path / "2024" / "who-put-this-here.HEIC"]


def test_a_backup_stays_put_once_icloud_forgets_it(tmp_path: Path) -> None:
    """Freed assets are the point of the exercise; nothing may reclaim their files."""
    (tmp_path / "2024").mkdir()
    (tmp_path / "2024" / "IMG_1~gone.HEIC").write_bytes(b"1234")

    index, plan = _plan_for([], tmp_path, holding(("gone", "2024/IMG_1~gone.HEIC", 4)))

    assert index.stray == [], "a record still claims it even with the asset gone from iCloud"
    assert plan.is_empty


# --- applying ---------------------------------------------------------------


def test_a_download_lands_on_the_drive_and_is_recorded(
    tmp_path: Path, conn: sqlite3.Connection, icloud: dict[tuple[str, str], bytes]
) -> None:
    icloud[("id-1", "original")] = b"the original bytes"
    photo = make_photo("id-1", "IMG_1.HEIC", size=18)
    library.replace_assets(conn, [photo])
    _, plan = _plan_for([photo], tmp_path)

    outcome = apply_plan(plan, tmp_path, conn, api=object())

    landed = tmp_path / "2024" / "IMG_1~id-1.HEIC"
    assert outcome.downloaded == 1 and outcome.failed == 0
    assert landed.read_bytes() == b"the original bytes"
    [item] = library.backed_up(conn)["id-1"].files
    assert item.relative_path == "2024/IMG_1~id-1.HEIC"
    assert item.sha256 == backup.sha256(landed)


def test_both_halves_of_a_live_photo_land_under_one_record(
    tmp_path: Path, conn: sqlite3.Connection, icloud: dict[tuple[str, str], bytes]
) -> None:
    icloud[("id-1", "original")] = b"still"
    icloud[("id-1", "original_video")] = b"motion clip"
    photo = make_photo("id-1", "IMG_1.HEIC", size=5, live_size=11)
    library.replace_assets(conn, [photo])
    _, plan = _plan_for([photo], tmp_path)

    outcome = apply_plan(plan, tmp_path, conn, api=object())

    assert outcome.downloaded == 1
    assert (tmp_path / "2024" / "IMG_1~id-1.HEIC").read_bytes() == b"still"
    assert (tmp_path / "2024" / "IMG_1~id-1.MOV").read_bytes() == b"motion clip"
    asset = library.backed_up(conn)["id-1"]
    assert [item.relative_path for item in asset.files] == [
        "2024/IMG_1~id-1.HEIC",
        "2024/IMG_1~id-1.MOV",
    ]
    assert asset.total_bytes == 16


def test_a_short_download_is_discarded_rather_than_recorded(
    tmp_path: Path, conn: sqlite3.Connection, icloud: dict[tuple[str, str], bytes]
) -> None:
    """A truncated file that looks complete is worse than no file at all."""
    icloud[("id-1", "original")] = b"cut off"
    photo = make_photo("id-1", "IMG_1.HEIC", size=999)
    library.replace_assets(conn, [photo])
    _, plan = _plan_for([photo], tmp_path)

    outcome = apply_plan(plan, tmp_path, conn, api=object())

    assert outcome.downloaded == 0 and outcome.failed == 1
    assert not (tmp_path / "2024" / "IMG_1~id-1.HEIC").exists()
    assert library.backed_up(conn) == {}


def test_a_file_already_on_the_drive_at_the_right_size_is_recorded_not_refetched(
    tmp_path: Path, conn: sqlite3.Connection, icloud: dict[tuple[str, str], bytes]
) -> None:
    """A run interrupted between writing and recording must cost bookkeeping,
    never 25 GB of re-downloading."""
    (tmp_path / "2024").mkdir()
    (tmp_path / "2024" / "IMG_1~id-1.HEIC").write_bytes(b"already here")
    icloud[("id-1", "original")] = b"WOULD BE RE-FETCHED"
    photo = make_photo("id-1", "IMG_1.HEIC", size=12)
    library.replace_assets(conn, [photo])
    plan = build_plan(build_index([photo], {}, {}, tmp_path), tmp_path)

    outcome = apply_plan(plan, tmp_path, conn, api=object())

    assert outcome.downloaded == 1
    assert (tmp_path / "2024" / "IMG_1~id-1.HEIC").read_bytes() == b"already here"
    assert conn.execute("SELECT recorded_as FROM files").fetchone()[0] == "found on the drive"


def test_a_file_on_the_drive_at_the_wrong_size_is_never_overwritten(
    tmp_path: Path, conn: sqlite3.Connection, icloud: dict[tuple[str, str], bytes]
) -> None:
    """Unexplained bytes at a backup's path are damage to look at, not to clobber."""
    (tmp_path / "2024").mkdir()
    (tmp_path / "2024" / "IMG_1~id-1.HEIC").write_bytes(b"something else entirely")
    icloud[("id-1", "original")] = b"the real bytes"
    photo = make_photo("id-1", "IMG_1.HEIC", size=14)
    library.replace_assets(conn, [photo])
    plan = build_plan(build_index([photo], {}, {}, tmp_path), tmp_path)

    outcome = apply_plan(plan, tmp_path, conn, api=object())

    assert outcome.failed == 1
    assert (tmp_path / "2024" / "IMG_1~id-1.HEIC").read_bytes() == b"something else entirely"


def test_an_asset_missing_from_every_album_counts_as_failed(
    tmp_path: Path, conn: sqlite3.Connection, icloud: dict[tuple[str, str], bytes]
) -> None:
    photo = make_photo("id-1", "IMG_1.HEIC")
    library.replace_assets(conn, [photo])
    _, plan = _plan_for([photo], tmp_path)

    outcome = apply_plan(plan, tmp_path, conn, api=object())

    assert outcome.downloaded == 0 and outcome.failed == 1


def test_a_download_error_is_counted_not_raised(
    tmp_path: Path,
    conn: sqlite3.Connection,
    icloud: dict[tuple[str, str], bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad asset must not abandon the other 9,616."""
    icloud[("id-1", "original")] = b"fine"
    icloud[("id-2", "original")] = b"never read"

    def explode(api: object, asset: FakeAsset, destination: Path, *, version: str) -> int:
        if asset.id == "id-2":
            raise DownloadError("id-2: the stream died")
        destination.write_bytes(icloud[(asset.id, version)])
        return len(icloud[(asset.id, version)])

    monkeypatch.setattr(backup, "download_original", explode)
    photos = [make_photo("id-1", "IMG_1.HEIC", size=4), make_photo("id-2", "IMG_2.HEIC", size=10)]
    library.replace_assets(conn, photos)
    _, plan = _plan_for(photos, tmp_path)

    outcome = apply_plan(plan, tmp_path, conn, api=object())

    assert outcome.downloaded == 1 and outcome.failed == 1


def test_a_run_of_failures_re_opens_the_session_and_resumes(
    tmp_path: Path,
    conn: sqlite3.Connection,
    icloud: dict[tuple[str, str], bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired session is worth re-opening, not worth abandoning 9,600 downloads for."""
    monkeypatch.setattr(backup, "_MAX_CONSECUTIVE_FAILURES", 3)
    for n in range(20):
        icloud[(f"id-{n:02d}", "original")] = b"good"
    expired = [False]

    def dies_once_the_session_expires(
        api: object, asset: FakeAsset, destination: Path, *, version: str
    ) -> int:
        if expired[0]:
            raise DownloadError(f"{asset.id}: session expired")
        destination.write_bytes(b"good")
        return 4

    def re_authenticate() -> object:
        expired[0] = False
        return object()

    monkeypatch.setattr(backup, "download_original", dies_once_the_session_expires)
    monkeypatch.setattr(backup, "authenticate", re_authenticate)
    photos = [make_photo(f"id-{n:02d}", f"IMG_{n}.HEIC", size=4) for n in range(20)]
    library.replace_assets(conn, photos)
    _, plan = _plan_for(photos, tmp_path)

    expired[0] = True
    outcome = apply_plan(plan, tmp_path, conn, api=object())

    assert not outcome.aborted
    assert outcome.sessions_reopened >= 1, "the run of failures should have re-opened the session"
    assert outcome.downloaded == 20 and outcome.failed == 0
    assert len(library.backed_up(conn)) == 20


def test_a_session_that_cannot_be_recovered_stops_but_keeps_what_landed(
    tmp_path: Path,
    conn: sqlite3.Connection,
    icloud: dict[tuple[str, str], bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-authenticating forever is worse than reporting a dead session once."""
    monkeypatch.setattr(backup, "_MAX_CONSECUTIVE_FAILURES", 3)
    for n in range(20):
        icloud[(f"id-{n:02d}", "original")] = b"good"
    calls = []

    def only_the_first_ever_works(api: object, asset: FakeAsset, destination: Path, *, version: str) -> int:
        calls.append(asset.id)
        if asset.id != "id-00":
            raise DownloadError(f"{asset.id}: session expired")
        destination.write_bytes(b"good")
        return 4

    monkeypatch.setattr(backup, "download_original", only_the_first_ever_works)
    photos = [make_photo(f"id-{n:02d}", f"IMG_{n}.HEIC", size=4) for n in range(20)]
    library.replace_assets(conn, photos)
    _, plan = _plan_for(photos, tmp_path)

    outcome = apply_plan(plan, tmp_path, conn, api=object())

    assert outcome.downloaded == 1 and outcome.failed == 19
    assert len(calls) <= 20 * backup._MAX_ASSET_ATTEMPTS, "no asset is retried without limit"
    assert library.backed_up(conn)["id-00"].files[0].relative_path == "2024/IMG_0~id-00.HEIC"


def test_a_crawl_that_loses_the_session_re_opens_it_and_resumes(
    tmp_path: Path,
    conn: sqlite3.Connection,
    icloud: dict[tuple[str, str], bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apple resets the connection mid-walk; that is a session to re-open, not a crash."""
    for n in range(4):
        icloud[(f"id-{n}", "original")] = b"good"
    crawls = [0]

    def find_assets(api: object, wanted_ids: set[str], albums: set[str], **kwargs: object):
        crawls[0] += 1
        for asset_id in sorted(wanted_ids):
            if crawls[0] == 1 and asset_id == "id-2":
                raise RuntimeError("Connection reset by peer")
            yield FakeAsset(id=asset_id, filename=f"{asset_id}.HEIC")

    monkeypatch.setattr(backup, "find_assets", find_assets)
    photos = [make_photo(f"id-{n}", f"IMG_{n}.HEIC", size=4) for n in range(4)]
    library.replace_assets(conn, photos)
    _, plan = _plan_for(photos, tmp_path)

    outcome = apply_plan(plan, tmp_path, conn, api=object())

    assert outcome.downloaded == 4 and outcome.failed == 0
    assert outcome.sessions_reopened == 1


def test_what_landed_before_a_crash_stays_recorded(
    tmp_path: Path,
    conn: sqlite3.Connection,
    icloud: dict[tuple[str, str], bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each asset is committed as it lands, so an unexpected crash loses none of them."""
    icloud[("id-1", "original")] = b"good"
    icloud[("id-2", "original")] = b"good"

    def blow_up_on_the_second(api: object, asset: FakeAsset, destination: Path, *, version: str) -> int:
        if asset.id == "id-2":
            raise RuntimeError("something nobody anticipated")
        destination.write_bytes(b"good")
        return 4

    monkeypatch.setattr(backup, "download_original", blow_up_on_the_second)
    photos = [make_photo("id-1", "IMG_1.HEIC", size=4), make_photo("id-2", "IMG_2.HEIC", size=4)]
    library.replace_assets(conn, photos)
    _, plan = _plan_for(photos, tmp_path)

    with pytest.raises(RuntimeError):
        apply_plan(plan, tmp_path, conn, api=object())

    assert "id-1" in library.backed_up(conn)


def test_rerun_after_apply_is_a_no_op(
    tmp_path: Path, conn: sqlite3.Connection, icloud: dict[tuple[str, str], bytes]
) -> None:
    """The plan is desired state, so applying it twice must download nothing."""
    icloud[("id-1", "original")] = b"the original bytes"
    photo = make_photo("id-1", "IMG_1.HEIC", size=18)
    library.replace_assets(conn, [photo])
    _, first = _plan_for([photo], tmp_path)
    apply_plan(first, tmp_path, conn, api=object())

    index, second = _plan_for([photo], tmp_path, library.backed_up(conn))

    assert index.recorded == ["id-1"]
    assert second.is_empty


# --- the commands -----------------------------------------------------------


@pytest.mark.parametrize(
    ("damage", "expected_exit"),
    [(None, 0), ("delete", 1), ("truncate", 1), ("rewrite", 1)],
    ids=["intact", "file_deleted", "size_changed", "hash_changed"],
)
def test_verify_fails_only_on_damage_to_what_the_library_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str | None, expected_exit: int
) -> None:
    photo = tmp_path / "2024" / "IMG_1.HEIC"
    photo.parent.mkdir()
    photo.write_bytes(b"the original bytes")
    # An asset with no backup yet is reported, but must not fail the check:
    # `download` is what answers that question.
    seed(tmp_path, make_photo("id-1", "IMG_1.HEIC"), make_photo("id-2", "IMG_2.HEIC"))
    record(tmp_path, "id-1", photo)

    if damage == "delete":
        photo.unlink()
    elif damage == "truncate":
        photo.write_bytes(b"short")
    elif damage == "rewrite":
        photo.write_bytes(b"different bytes!!!")

    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)
    result = CliRunner().invoke(app, ["verify"])

    assert result.exit_code == expected_exit
    assert "not backed up yet" in result.output


def test_a_clean_verify_refreshes_the_plain_text_copy_of_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = tmp_path / "2024" / "IMG_1.HEIC"
    photo.parent.mkdir()
    photo.write_bytes(b"the original bytes")
    seed(tmp_path, make_photo("id-1", "IMG_1.HEIC"))
    record(tmp_path, "id-1", photo)
    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)

    result = CliRunner().invoke(app, ["verify"])

    assert result.exit_code == 0
    assert "id-1" in (tmp_path / library.DUMP_NAME).read_text()


def test_a_failed_verify_leaves_the_last_good_dump_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy taken while it was whole is the one worth keeping."""
    photo = tmp_path / "2024" / "IMG_1.HEIC"
    photo.parent.mkdir()
    photo.write_bytes(b"the original bytes")
    seed(tmp_path, make_photo("id-1", "IMG_1.HEIC"))
    record(tmp_path, "id-1", photo)
    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)
    CliRunner().invoke(app, ["verify"])
    good = (tmp_path / library.DUMP_NAME).read_text()

    photo.write_bytes(b"different bytes!!!")
    result = CliRunner().invoke(app, ["verify"])

    assert result.exit_code == 1
    assert (tmp_path / library.DUMP_NAME).read_text() == good


def test_the_dump_is_not_mistaken_for_media_on_the_drive(tmp_path: Path) -> None:
    """It sits beside the media, so a scan that counted it would call it stray."""
    (tmp_path / library.DUMP_NAME).write_text("-- sql")

    index, _ = _plan_for([], tmp_path)

    assert index.stray == []


def test_verify_checks_both_halves_of_a_live_photo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing the motion clip is real data loss and must not verify green."""
    still = tmp_path / "2024" / "IMG_1.HEIC"
    motion = tmp_path / "2024" / "IMG_1.MOV"
    still.parent.mkdir()
    still.write_bytes(b"still")
    motion.write_bytes(b"motion")
    seed(tmp_path, make_photo("id-1", "IMG_1.HEIC", live_size=6))
    record(tmp_path, "id-1", still, motion)
    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)
    assert CliRunner().invoke(app, ["verify"]).exit_code == 0

    motion.unlink()
    result = CliRunner().invoke(app, ["verify"])

    assert result.exit_code == 1
    assert "missing: 2024/IMG_1.MOV" in result.output


def test_verify_reports_which_backups_icloud_no_longer_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup-only records are the goal of the exercise, not a discrepancy.

    `verify` has to name them: this drive is their only copy.
    """
    seed(
        tmp_path,
        make_photo("still-live", "A.HEIC"),
        make_photo("not-yet-backed-up", "B.HEIC"),
        freed=(make_photo("freed-1", "C.HEIC"), make_photo("freed-2", "D.HEIC")),
    )
    for asset_id in ("still-live", "freed-1", "freed-2"):
        path = tmp_path / f"{asset_id}.HEIC"
        path.write_bytes(b"x")
        record(tmp_path, asset_id, path)
    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)

    result = CliRunner().invoke(app, ["verify"])

    assert result.exit_code == 0
    assert "1 still in iCloud and backed up" in result.output
    assert "2 backup-only, no longer in iCloud" in result.output
    assert "1 iCloud assets not backed up yet" in result.output


def stub_pull(monkeypatch: pytest.MonkeyPatch, result: PullResult) -> list[object]:
    """Stand in for the metadata refresh, recording the session it was handed."""
    sessions: list[object] = []

    def refresh(_conn: object, api: object, **_kwargs: object) -> PullResult:
        sessions.append(api)
        return result

    monkeypatch.setattr(backup, "authenticate", lambda: "session")
    monkeypatch.setattr(backup, "refresh_incremental", refresh)
    return sessions


def test_plan_refreshes_the_metadata_before_planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refresh is not optional, so a plan is never built against a stale view."""
    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)
    sessions = stub_pull(monkeypatch, PullResult(9, 0))

    result = CliRunner().invoke(app, ["plan"])

    assert result.exit_code == 0
    assert sessions == ["session"]


def test_download_refuses_to_plan_against_an_incomplete_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pages lost to a flaky connection could hide whole assets from the plan."""
    scanned: list[object] = []
    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)
    stub_pull(monkeypatch, PullResult(0, 3))
    monkeypatch.setattr(backup, "scan", lambda *_, **__: scanned.append(1) or {})

    result = CliRunner().invoke(app, ["download"])

    assert result.exit_code == 1
    assert scanned == [], "aborted before even reading the drive"


def test_download_exits_non_zero_when_a_download_fails(
    tmp_path: Path, icloud: dict[tuple[str, str], bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download that half-worked must not look like a successful backup."""
    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)
    stub_pull(monkeypatch, PullResult(1, 0))
    seed(tmp_path, make_photo("id-1", "IMG_1.HEIC"))

    result = CliRunner().invoke(app, ["download"])

    assert result.exit_code == 1


def test_download_reports_what_the_sync_could_not_fetch(
    tmp_path: Path, icloud: dict[tuple[str, str], bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apply_plan` only counts; naming the casualties is the command's job."""
    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)
    stub_pull(monkeypatch, PullResult(1, 0))
    seed(tmp_path, make_photo("id-1", "IMG_1.HEIC"))

    result = CliRunner().invoke(app, ["download"])

    assert result.exit_code == 1
    assert "IMG_1.HEIC: not found in any album" in result.output


def test_download_says_so_when_the_sync_gave_up_on_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that stopped early must not read like one that finished."""
    monkeypatch.setattr(cli, "DEFAULT_DIR", tmp_path)
    stub_pull(monkeypatch, PullResult(1, 0))
    seed(tmp_path, make_photo("id-1", "IMG_1.HEIC"))
    monkeypatch.setattr(
        cli, "apply_plan", lambda *_, **__: Outcome(downloaded=2, failed=7, aborted=True, remaining=7)
    )

    result = CliRunner().invoke(app, ["download"])

    assert result.exit_code == 1
    assert "Gave up" in result.output and "7 to go" in result.output
    assert "not found in any album" not in result.output, "aborted early, so nothing is known missing"
