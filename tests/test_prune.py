"""Deleting is the one thing here that cannot be undone by running it again.

So what these pin is the refusals. An `icloud` mark exists to free space while
the drive keeps the photo, and it is only worth anything if the drive really
does hold it: every way that can be false has a test. The happy path is one
test; the ways to lose a photo are the rest.
"""

import sqlite3
from pathlib import Path

import pytest

from icloud_photo_metadata import library
from icloud_photo_metadata import prune
from icloud_photo_metadata import queries
from icloud_photo_metadata.drive import sha256
from icloud_photo_metadata.library import BackedUpFile
from tests.icloud_factories import make_photo

BYTES = b"the original, as downloaded"


@pytest.fixture
def drive(tmp_path: Path) -> Path:
    """A drive holding `kept`, whose original is really there and really matches.

    `unbacked` is an asset iCloud has and the drive does not, which is the state
    an `icloud` mark must refuse.
    """
    directory = tmp_path / "drive"
    (directory / "2024").mkdir(parents=True)
    original = directory / "2024" / "IMG_1~kept.HEIC"
    original.write_bytes(BYTES)

    with library.library(directory) as conn:
        library.replace_assets(
            conn,
            [
                make_photo("kept", "IMG_1.HEIC", size=len(BYTES)),
                make_photo("unbacked", "IMG_2.HEIC", size=4_000),
            ],
        )
        library.record_files(
            conn,
            "kept",
            [BackedUpFile(library.ORIGINAL, "2024/IMG_1~kept.HEIC", len(BYTES), sha256(original))],
            recorded_as="downloaded",
        )
    return directory


class FakeICloud:
    """Stands in for the two calls a prune makes against Apple.

    A real one costs a CloudKit lookup and a write per asset; what the decision
    layer needs to know is only which ids were deleted, and what happens when
    one of them will not go.
    """

    def __init__(self, *, refuses: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self._refuses = refuses or set()

    def fetch(self, api: object, asset_id: str, album: str) -> str:
        return asset_id

    def delete(self, asset_id: str) -> None:
        if asset_id in self._refuses:
            raise prune.DeleteFailed(f"{asset_id}: iCloud said no")
        self.deleted.append(asset_id)


@pytest.fixture
def icloud(monkeypatch: pytest.MonkeyPatch) -> FakeICloud:
    fake = FakeICloud()
    monkeypatch.setattr(prune, "fetch_asset", fake.fetch)
    monkeypatch.setattr(prune, "delete_asset", fake.delete)
    return fake


def plan_for(directory: Path, asset_id: str, mark: str) -> prune.PrunePlan:
    with library.library(directory) as conn:
        queries.set_mark(conn, asset_id, mark)
        return prune.build_plan(conn, directory)


def test_an_icloud_mark_whose_copy_is_on_the_drive_is_ready(drive: Path) -> None:
    plan = plan_for(drive, "kept", queries.ICLOUD)

    assert [target.asset_id for target in plan.ready] == ["kept"]
    assert plan.blocked == ()
    # The drive keeps its copy, so this frees nothing there.
    assert plan.drive_bytes == 0


def test_an_icloud_mark_on_an_asset_the_drive_never_held_is_refused(drive: Path) -> None:
    plan = plan_for(drive, "unbacked", queries.ICLOUD)

    assert plan.is_empty
    assert "not backed up" in plan.blocked[0].reason


def test_an_icloud_mark_is_refused_once_the_file_is_gone(drive: Path) -> None:
    (drive / "2024" / "IMG_1~kept.HEIC").unlink()

    plan = plan_for(drive, "kept", queries.ICLOUD)

    assert plan.is_empty
    assert "missing from the drive" in plan.blocked[0].reason


def test_an_icloud_mark_is_refused_when_the_bytes_have_changed(drive: Path) -> None:
    """Same length, different content: only the hash catches this one."""
    (drive / "2024" / "IMG_1~kept.HEIC").write_bytes(b"X" * len(BYTES))

    plan = plan_for(drive, "kept", queries.ICLOUD)

    assert plan.is_empty
    assert "does not hash" in plan.blocked[0].reason


def test_an_everywhere_mark_needs_no_backup_to_fall_back_on(drive: Path) -> None:
    """Discarding the copy is the request, so there is nothing to verify."""
    plan = plan_for(drive, "unbacked", queries.EVERYWHERE)

    assert [target.asset_id for target in plan.ready] == ["unbacked"]


def test_an_asset_already_gone_from_everywhere_is_not_deleted_again(drive: Path) -> None:
    with library.library(drive) as conn:
        queries.set_mark(conn, "unbacked", queries.EVERYWHERE)
        library.record_gone(conn, "unbacked")
        plan = prune.build_plan(conn, drive)

    assert plan.is_empty
    assert "already gone" in plan.blocked[0].reason


def test_an_icloud_prune_frees_icloud_and_leaves_the_drive_alone(drive: Path, icloud: FakeICloud) -> None:
    original = drive / "2024" / "IMG_1~kept.HEIC"
    with library.library(drive) as conn:
        queries.set_mark(conn, "kept", queries.ICLOUD)
        outcome = prune.apply_plan(prune.build_plan(conn, drive), drive, conn, api=None)

    assert icloud.deleted == ["kept"]
    assert outcome.freed_icloud == ("kept",)
    assert outcome.freed_drive == ()
    assert original.read_bytes() == BYTES

    with library.reading(drive) as conn:
        assert _in_icloud(conn, "kept") == 0
        # Nothing left to do, and the library still describes the photo it holds.
        assert queries.pending_marks(conn) == []
        assert queries.original_path(conn, "kept") == "2024/IMG_1~kept.HEIC"


def test_an_everywhere_prune_takes_the_drive_copy_too(drive: Path, icloud: FakeICloud) -> None:
    original = drive / "2024" / "IMG_1~kept.HEIC"
    with library.library(drive) as conn:
        queries.set_mark(conn, "kept", queries.EVERYWHERE)
        outcome = prune.apply_plan(prune.build_plan(conn, drive), drive, conn, api=None)

    assert outcome.freed_drive == ("kept",)
    assert not original.exists()

    with library.reading(drive) as conn:
        # The row outlives the bytes: it is the only record the photo existed.
        assert queries.original_path(conn, "kept") is None
        assert queries.get_asset(conn, "kept")["filename"] == "IMG_1.HEIC"


def test_a_refused_icloud_delete_leaves_the_asset_and_the_mark_alone(
    drive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drive's copy must survive a failure, and the mark must still be pending."""
    fake = FakeICloud(refuses={"kept"})
    monkeypatch.setattr(prune, "fetch_asset", fake.fetch)
    monkeypatch.setattr(prune, "delete_asset", fake.delete)

    with library.library(drive) as conn:
        queries.set_mark(conn, "kept", queries.EVERYWHERE)
        outcome = prune.apply_plan(prune.build_plan(conn, drive), drive, conn, api=None)

    assert [asset_id for asset_id, _ in outcome.failures] == ["kept"]
    assert (drive / "2024" / "IMG_1~kept.HEIC").exists()

    with library.reading(drive) as conn:
        assert [row["asset_id"] for row in queries.pending_marks(conn)] == ["kept"]
        assert _in_icloud(conn, "kept") == 1


def test_a_plan_can_be_held_to_one_level(drive: Path) -> None:
    with library.library(drive) as conn:
        queries.set_mark(conn, "kept", queries.ICLOUD)
        queries.set_mark(conn, "unbacked", queries.EVERYWHERE)

        assert [target.asset_id for target in prune.build_plan(conn, drive).ready] == ["kept", "unbacked"]
        held = prune.build_plan(conn, drive, mark=queries.EVERYWHERE)

    assert [target.asset_id for target in held.ready] == ["unbacked"]


def test_re_marking_an_executed_asset_makes_it_pending_again(drive: Path, icloud: FakeICloud) -> None:
    """An asset recovered from Recently Deleted and marked again is a fresh decision."""
    with library.library(drive) as conn:
        queries.set_mark(conn, "kept", queries.ICLOUD)
        prune.apply_plan(prune.build_plan(conn, drive), drive, conn, api=None)
        assert queries.pending_marks(conn) == []

        queries.set_mark(conn, "kept", queries.EVERYWHERE)

        assert [row["mark"] for row in queries.pending_marks(conn)] == [queries.EVERYWHERE]


def _in_icloud(conn: sqlite3.Connection, asset_id: str) -> int:
    return int(conn.execute("SELECT in_icloud FROM assets WHERE id = ?", (asset_id,)).fetchone()[0])
