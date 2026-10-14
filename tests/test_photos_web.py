"""The photo routes: what they serve, what they refuse, and what they render.

The library sits on a removable drive, so the case worth pinning hardest is the
one where it is not there: the page has to say so rather than fail as a server
error. The image route is exercised against real bytes, because the whole
reason it exists is that a browser cannot open the originals.
"""

import io
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient
from PIL import Image

from icloud_photo_metadata import library
from icloud_photo_metadata import queries
from icloud_photo_metadata import prune as photos_prune
from icloud_photo_metadata.backup import Download
from icloud_photo_metadata.backup import MetadataPullFailed
from icloud_photo_metadata.backup import Outcome
from icloud_photo_metadata.backup import Survey
from icloud_photo_metadata.backup import SyncPlan
from icloud_photo_metadata.library import BackedUpFile
from icloud_photo_metadata.models import PhotoMetadata
from icloud_photo_metadata.pull_metadata import PullResult
from src.web.app import create_app
from src.web.photos import api as photos_api
from src.web.photos import jobs as photos_jobs
from src.web.photos import pruning as photos_pruning
from src.web.photos import sync as photos_sync
from src.web.photos import thumbnails
from tests.icloud_factories import make_photo


def _library_assets(drive: Path) -> list[PhotoMetadata]:
    """What iCloud holds in the fixture: a backed-up still, a movie, and a gap."""
    return [
        make_photo("still", "IMG_1.HEIC", size=(drive / "2024" / "IMG_1~still.HEIC").stat().st_size),
        make_photo("clip", "IMG_2.MOV", item_type="movie"),
        make_photo("unbacked", "IMG_3.HEIC"),
    ]


def _alongside(drive: Path, photo: PhotoMetadata) -> None:
    """Add one asset the way a pull does, leaving the fixture's own in iCloud.

    `replace_assets` records a crawl of everything, so whatever it is not handed
    has left iCloud -- which is why the fixture's three go back in with it.
    """
    with library.library(drive) as conn:
        library.replace_assets(conn, [*_library_assets(drive), photo])


@pytest.fixture
def drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A backup drive holding one real JPEG, one video and one unbacked asset."""
    directory = tmp_path / "drive"
    (directory / "2024").mkdir(parents=True)
    original = directory / "2024" / "IMG_1~still.HEIC"
    Image.new("RGB", (1200, 900), "teal").save(original, "JPEG")

    with library.library(directory) as conn:
        library.replace_assets(conn, _library_assets(directory))
        library.record_files(
            conn,
            "still",
            [BackedUpFile(library.ORIGINAL, "2024/IMG_1~still.HEIC", original.stat().st_size, "hash")],
            recorded_as="downloaded",
        )

    monkeypatch.setattr(photos_api, "DEFAULT_DIR", directory)
    monkeypatch.setattr(thumbnails, "CACHE_DIR", tmp_path / "thumbs")
    return directory


@pytest.fixture
def client(drive: Path) -> Iterator[FlaskClient]:
    with create_app(start_poller=False).test_client() as test_client:
        yield test_client


def _back_up_the_clip(drive: Path, contents: bytes = b"a movie") -> Path:
    """Put the fixture's movie on the drive and record it as backed up."""
    movie = drive / "2024" / "IMG_2~clip.MOV"
    movie.write_bytes(contents)
    with library.library(drive) as conn:
        library.record_files(
            conn,
            "clip",
            [BackedUpFile(library.ORIGINAL, "2024/IMG_2~clip.MOV", movie.stat().st_size, "hash")],
            recorded_as="downloaded",
        )
    return movie


def test_the_gallery_lists_every_asset_with_what_a_card_needs(client: FlaskClient) -> None:
    body = client.get("/api/photos/assets").get_json()

    assert body["total"] == 3
    assert {asset["filename"] for asset in body["assets"]} == {"IMG_1.HEIC", "IMG_2.MOV", "IMG_3.HEIC"}
    assert body["next_offset"] is None


def test_a_filtered_page_reports_the_size_of_everything_it_matched(client: FlaskClient) -> None:
    """A count alone does not say whether pruning a filter is worth doing."""
    movies = client.get("/api/photos/assets?type=movie").get_json()
    whole = client.get("/api/photos/assets").get_json()

    assert movies["total_bytes"] == 1000
    assert whole["total_bytes"] > movies["total_bytes"]


def test_filters_reach_the_query_layer_from_the_query_string(client: FlaskClient) -> None:
    assert client.get("/api/photos/assets?type=movie").get_json()["total"] == 1
    assert client.get("/api/photos/assets?backed_up=true").get_json()["total"] == 1


def test_an_unknown_sort_is_a_bad_request_not_a_broken_query(client: FlaskClient) -> None:
    assert client.get("/api/photos/assets?sort=; DROP TABLE assets").status_code == 400


def test_an_original_is_served_as_a_jpeg_the_browser_can_show(client: FlaskClient) -> None:
    """The library is 76% HEIC, so serving the original bytes would render nothing."""
    response = client.get("/api/photos/assets/still/image?size=grid")

    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert max(Image.open(io.BytesIO(response.data)).size) == thumbnails.SIZES["grid"]


def test_an_asset_with_no_file_on_the_drive_has_no_image(client: FlaskClient) -> None:
    assert client.get("/api/photos/assets/unbacked/image").status_code == 404


def test_an_unknown_size_is_refused(client: FlaskClient) -> None:
    assert client.get("/api/photos/assets/still/image?size=enormous").status_code == 400


def test_a_record_whose_file_has_vanished_reads_as_missing(client: FlaskClient, drive: Path) -> None:
    """`verify` is what calls this damage; a page just has nothing to show."""
    (drive / "2024" / "IMG_1~still.HEIC").unlink()

    assert client.get("/api/photos/assets/still/image").status_code == 404


def test_marking_an_asset_survives_and_shows_up_in_the_totals(client: FlaskClient) -> None:
    assert client.put("/api/photos/assets/still/mark", json={"mark": "icloud"}).status_code == 200

    assert client.get("/api/photos/assets?mark=icloud").get_json()["total"] == 1
    assert client.get("/api/photos/facets").get_json()["totals"]["marked"] == 1

    client.put("/api/photos/assets/still/mark", json={"mark": None})

    assert client.get("/api/photos/assets?mark=icloud").get_json()["total"] == 0


def test_the_gallery_can_show_everything_marked_anywhere(client: FlaskClient) -> None:
    """The question you ask before pruning: what did I flag, all of it?"""
    client.put("/api/photos/assets/still/mark", json={"mark": "icloud"})
    client.put("/api/photos/assets/clip/mark", json={"mark": "everywhere"})
    client.put("/api/photos/assets/unbacked/mark", json={"mark": "drive"})

    assert client.get("/api/photos/assets?mark=any").get_json()["total"] == 3
    assert client.get("/api/photos/assets?mark=icloud").get_json()["total"] == 1
    assert client.get("/api/photos/assets?mark=drive").get_json()["total"] == 1
    assert client.get("/api/photos/assets?mark=everywhere").get_json()["total"] == 1
    assert client.get("/api/photos/assets").get_json()["total"] == 3


def test_the_queue_lists_what_is_waiting_on_a_prune(client: FlaskClient) -> None:
    """What the confirmation shows before deleting: the decisions, not a count."""
    client.put("/api/photos/assets/clip/mark", json={"mark": "everywhere"})
    client.put("/api/photos/assets/still/mark", json={"mark": "icloud"})

    marks = client.get("/api/photos/marks").get_json()["marks"]

    assert [(row["asset_id"], row["mark"], row["filename"]) for row in marks] == [
        ("clip", "everywhere", "IMG_2.MOV"),
        ("still", "icloud", "IMG_1.HEIC"),
    ]

    client.put("/api/photos/assets/clip/mark", json={"mark": None})

    assert [row["asset_id"] for row in client.get("/api/photos/marks").get_json()["marks"]] == ["still"]


def test_a_mark_filter_the_library_does_not_define_is_refused(client: FlaskClient) -> None:
    assert client.get("/api/photos/assets?mark=burn").status_code == 400


@pytest.mark.parametrize("body", [{"mark": "burn"}, {}, []], ids=["unknown", "empty", "not_an_object"])
def test_a_mark_the_library_does_not_define_is_refused(client: FlaskClient, body: object) -> None:
    assert client.put("/api/photos/assets/still/mark", json=body).status_code == 400


def test_marking_an_asset_that_does_not_exist_is_a_404(client: FlaskClient) -> None:
    assert client.put("/api/photos/assets/never-existed/mark", json={"mark": "icloud"}).status_code == 404


def test_facets_offer_the_filters_this_library_can_answer(client: FlaskClient) -> None:
    body = client.get("/api/photos/facets").get_json()

    assert {item["value"] for item in body["facets"]["types"]} == {"image", "movie"}
    assert body["totals"]["assets"] == 3


def test_an_unmounted_drive_reads_as_unavailable_rather_than_broken(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The drive is USB. Unplugged is a state the page explains, not a 500."""
    monkeypatch.setattr(photos_api, "DEFAULT_DIR", tmp_path / "not-mounted")

    assert client.get("/api/photos/assets").status_code == 503
    assert client.get("/api/photos/facets").status_code == 503


def test_the_page_renders_and_joins_the_nav(client: FlaskClient) -> None:
    page = client.get("/photos")

    assert page.status_code == 200
    assert b"gallery.js" in page.data
    assert b'href="/photos"' in page.data


@pytest.fixture
def refresh(monkeypatch: pytest.MonkeyPatch) -> photos_sync.Refresh:
    """A sync of this process's own, so one test's run is not another's history."""
    fresh = photos_sync.Refresh()
    monkeypatch.setattr(photos_api, "refresh", fresh)
    return fresh


def _survey(directory: Path, downloads: int = 0) -> Survey:
    """A look at iCloud that found `downloads` assets the drive does not hold."""
    plan = SyncPlan(
        downloads=[
            Download(
                photo=make_photo(f"new{index}", f"IMG_{index}.HEIC"),
                destination=directory / f"IMG_{index}.HEIC",
                live_destination=None,
            )
            for index in range(downloads)
        ]
    )
    return Survey(
        plan=plan,
        index=None,  # type: ignore[arg-type]  -- the refresh only reads the plan
        api="session",
        in_icloud=[],
        pulled=PullResult(written=downloads, failed_pages=0),
        files_on_drive=0,
    )


def _settled(job: photos_jobs.BackgroundJob, timeout: float = 5.0) -> dict[str, Any]:
    """The state a background job came to rest in."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = job.state()
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError(f"the {type(job).__name__.lower()} never finished")


def test_a_refresh_downloads_what_the_drive_is_missing_and_says_what_it_got(
    client: FlaskClient, drive: Path, refresh: photos_sync.Refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(photos_sync, "survey", lambda *_, **__: _survey(drive, downloads=2))
    monkeypatch.setattr(photos_sync, "apply_plan", lambda *_, **__: Outcome(downloaded=2))

    started = client.post("/api/photos/refresh")

    assert started.status_code == 202
    assert started.get_json()["status"] == "running"
    assert _settled(refresh)["message"] == "Downloaded 2."
    assert client.get("/api/photos/refresh").get_json()["status"] == "done"


def test_a_refresh_with_nothing_to_fetch_downloads_nothing(
    client: FlaskClient, drive: Path, refresh: photos_sync.Refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty plan is the normal case: most refreshes find iCloud unchanged."""
    downloaded = []
    monkeypatch.setattr(photos_sync, "survey", lambda *_, **__: _survey(drive))
    monkeypatch.setattr(photos_sync, "apply_plan", lambda *_, **__: downloaded.append(1))

    client.post("/api/photos/refresh")

    assert _settled(refresh)["message"] == "The drive already matches iCloud."
    assert downloaded == []


def test_a_second_refresh_is_refused_while_one_is_still_running(
    client: FlaskClient, drive: Path, refresh: photos_sync.Refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two syncs would fight over one iCloud session and one set of rows."""
    release = threading.Event()

    def blocking_survey(*_: object, **__: object) -> Survey:
        release.wait(timeout=5)
        return _survey(drive)

    monkeypatch.setattr(photos_sync, "survey", blocking_survey)
    client.post("/api/photos/refresh")

    second = client.post("/api/photos/refresh")
    release.set()

    assert second.status_code == 409
    _settled(refresh)


def test_a_refresh_that_fails_is_reported_rather_than_lost_with_its_thread(
    client: FlaskClient, refresh: photos_sync.Refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody sees the worker's traceback, so the state has to carry the failure."""

    def failing_survey(*_: object, **__: object) -> Survey:
        raise MetadataPullFailed(PullResult(written=0, failed_pages=3))

    monkeypatch.setattr(photos_sync, "survey", failing_survey)
    client.post("/api/photos/refresh")

    state = _settled(refresh)

    assert state["status"] == "error"
    assert "3 metadata pages failed" in state["message"]


def test_a_refresh_needs_the_drive_before_it_needs_icloud(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(photos_api, "DEFAULT_DIR", tmp_path / "not-mounted")

    assert client.post("/api/photos/refresh").status_code == 503


@pytest.fixture
def prune(monkeypatch: pytest.MonkeyPatch) -> photos_pruning.Prune:
    """A prune of this process's own, so one test's run is not another's history."""
    fresh = photos_pruning.Prune()
    monkeypatch.setattr(photos_api, "pruning", fresh)
    monkeypatch.setattr(photos_pruning, "authenticate", lambda: "session")
    return fresh


def test_a_prune_deletes_what_is_marked_and_says_what_it_took(
    client: FlaskClient, prune: photos_pruning.Prune, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(photos_prune, "fetch_asset", lambda _api, asset_id, _album: asset_id)
    monkeypatch.setattr(photos_prune, "delete_asset", deleted.append)
    client.put("/api/photos/assets/still/mark", json={"mark": "everywhere"})

    started = client.post("/api/photos/prune")

    assert started.status_code == 202
    assert _settled(prune)["message"].startswith("Deleted 1 from iCloud.")
    assert deleted == ["still"]


def test_a_prune_will_not_free_icloud_for_an_asset_the_drive_never_held(
    client: FlaskClient, prune: photos_pruning.Prune, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the level: it trades the iCloud copy for the drive's."""
    deleted: list[str] = []
    monkeypatch.setattr(photos_prune, "delete_asset", deleted.append)
    client.put("/api/photos/assets/unbacked/mark", json={"mark": "icloud"})

    client.post("/api/photos/prune")

    assert _settled(prune)["message"] == "Nothing was deleted: 1 marked asset did not pass the check."
    assert deleted == []
    # Still marked, so fixing the backup and re-running picks it up.
    assert client.get("/api/photos/facets").get_json()["totals"]["marked"] == 1


def test_a_prune_can_be_held_to_one_level_of_mark(
    client: FlaskClient, prune: photos_pruning.Prune, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(photos_prune, "fetch_asset", lambda _api, asset_id, _album: asset_id)
    monkeypatch.setattr(photos_prune, "delete_asset", deleted.append)
    client.put("/api/photos/assets/still/mark", json={"mark": "everywhere"})
    client.put("/api/photos/assets/clip/mark", json={"mark": "everywhere"})

    client.post("/api/photos/prune", json={"mark": "icloud"})

    assert _settled(prune)["message"] == "Nothing is marked."
    assert deleted == []


def test_a_prune_naming_a_mark_the_library_does_not_define_is_refused(client: FlaskClient) -> None:
    assert client.post("/api/photos/prune", json={"mark": "burn"}).status_code == 400


def test_a_second_prune_is_refused_while_one_is_still_running(
    client: FlaskClient, prune: photos_pruning.Prune, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two prunes would fight over one iCloud session and one set of marks."""
    release = threading.Event()

    def blocking_plan(*_: object, **__: object) -> photos_prune.PrunePlan:
        release.wait(timeout=5)
        return photos_prune.PrunePlan()

    monkeypatch.setattr(photos_prune, "build_plan", blocking_plan)
    client.post("/api/photos/prune")

    second = client.post("/api/photos/prune")
    release.set()

    assert second.status_code == 409


def test_a_prune_needs_the_drive_before_it_needs_icloud(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(photos_api, "DEFAULT_DIR", tmp_path / "not-mounted")

    assert client.post("/api/photos/prune").status_code == 503


def test_the_gallery_leaves_apples_trash_out_until_it_is_asked_for(client: FlaskClient, drive: Path) -> None:
    """A prune moves an asset here, so it comes back as a tile with no picture."""
    _alongside(drive, make_photo("binned", "IMG_9.HEIC", album="Recently Deleted"))

    assert client.get("/api/photos/assets").get_json()["total"] == 3
    assert client.get("/api/photos/assets?album=Recently+Deleted").get_json()["total"] == 1


def test_an_asset_gone_from_both_places_takes_no_slot_in_the_grid(
    client: FlaskClient, drive: Path
) -> None:
    """After an `everywhere` prune there is nothing left to draw or to delete.

    The row stays in the library, which is the only record the photo existed.
    What it must not be is a tile: a slot saying "gone" is one you cannot act
    on, and enough of them turn the grid into a list of what you no longer have.
    """

    def shown(query: str = "") -> list[str]:
        return sorted(row["id"] for row in client.get(f"/api/photos/assets{query}").get_json()["assets"])

    assert shown() == ["clip", "still", "unbacked"]

    with library.library(drive) as conn:
        library.record_gone(conn, "unbacked")

    assert shown() == ["clip", "still"]
    # No filter reaches it either: it is out of the gallery, not hidden behind
    # a control. The library still holds the row.
    assert shown("?in_icloud=false") == []
    with library.reading(drive) as conn:
        assert queries.get_asset(conn, "unbacked")["filename"] == "IMG_3.HEIC"


def test_a_movie_gets_a_frame_rather_than_a_placeholder(
    client: FlaskClient, drive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no still on the drive for a movie, so one is taken from it."""
    _back_up_the_clip(drive, b"not really a movie, ffmpeg is faked")

    frame = drive / "frame.jpg"
    Image.new("RGB", (1920, 1080), "purple").save(frame, "JPEG")
    monkeypatch.setattr(thumbnails, "_poster_frame", lambda _video: frame)

    response = client.get("/api/photos/assets/clip/image?size=grid")

    assert response.status_code == 200
    with Image.open(io.BytesIO(response.data)) as built:
        assert max(built.size) == thumbnails.SIZES["grid"]


def test_a_movie_with_no_ffmpeg_to_read_it_is_a_415(
    client: FlaskClient, drive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tile falls back to its own placeholder; the route must not 500."""
    _back_up_the_clip(drive)
    monkeypatch.setattr(thumbnails.shutil, "which", lambda _name: None)

    assert client.get("/api/photos/assets/clip/image?size=grid").status_code == 415


def test_a_movie_is_served_as_its_own_bytes_for_a_player_to_play(client: FlaskClient, drive: Path) -> None:
    """A .MOV plays as-is: converting one on request is not a page's wait."""
    contents = b"\x00\x00\x00\x14ftypqt   pretend this is H.264"
    _back_up_the_clip(drive, contents)

    response = client.get("/api/photos/assets/clip/video")

    assert response.status_code == 200
    assert response.mimetype == "video/quicktime"
    assert response.data == contents


def test_a_movie_serves_a_byte_range_so_a_player_can_seek(client: FlaskClient, drive: Path) -> None:
    """Safari will not start a video at all without a 206 to its range request."""
    _back_up_the_clip(drive, b"0123456789")

    response = client.get("/api/photos/assets/clip/video", headers={"Range": "bytes=2-5"})

    assert response.status_code == 206
    assert response.data == b"2345"


def test_a_photo_is_not_something_the_video_route_will_hand_out(client: FlaskClient) -> None:
    """Otherwise the route is a way to pull any original off the drive whole."""
    assert client.get("/api/photos/assets/still/video").status_code == 415


def test_a_movie_with_no_copy_on_the_drive_cannot_be_played(client: FlaskClient) -> None:
    assert client.get("/api/photos/assets/clip/video").status_code == 404


def test_a_card_says_whether_it_is_a_movie_a_browser_can_play(client: FlaskClient, drive: Path) -> None:
    """Apple files animated GIFs as movies, and those are stills to a browser."""
    _alongside(drive, make_photo("looping", "IMG_8.GIF", item_type="movie"))

    playable = {
        asset["id"]: asset["playable"] for asset in client.get("/api/photos/assets").get_json()["assets"]
    }

    assert playable == {"still": False, "clip": True, "unbacked": False, "looping": False}
    assert client.get("/api/photos/assets/clip").get_json()["playable"] is True
