"""The photo routes: what they serve, what they refuse, and what they render.

The library sits on a removable drive, so the case worth pinning hardest is the
one where it is not there: the page has to say so rather than fail as a server
error. The image route is exercised against real bytes, because the whole
reason it exists is that a browser cannot open the originals.
"""

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from PIL import Image

from icloud_photo_metadata import library
from icloud_photo_metadata.library import BackedUpFile
from src.web.app import create_app
from src.web.photos import api as photos_api
from src.web.photos import thumbnails
from tests.icloud_factories import make_photo


@pytest.fixture
def drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A backup drive holding one real JPEG, one video and one unbacked asset."""
    directory = tmp_path / "drive"
    (directory / "2024").mkdir(parents=True)
    original = directory / "2024" / "IMG_1~still.HEIC"
    Image.new("RGB", (1200, 900), "teal").save(original, "JPEG")

    with library.library(directory) as conn:
        library.replace_assets(
            conn,
            [
                make_photo("still", "IMG_1.HEIC", size=original.stat().st_size),
                make_photo("clip", "IMG_2.MOV", item_type="movie"),
                make_photo("unbacked", "IMG_3.HEIC"),
            ],
        )
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


def test_the_gallery_lists_every_asset_with_what_a_card_needs(client: FlaskClient) -> None:
    body = client.get("/api/photos/assets").get_json()

    assert body["total"] == 3
    assert {asset["filename"] for asset in body["assets"]} == {"IMG_1.HEIC", "IMG_2.MOV", "IMG_3.HEIC"}
    assert body["next_offset"] is None


def test_filters_reach_the_query_layer_from_the_query_string(client: FlaskClient) -> None:
    assert client.get("/api/photos/assets?type=movie").get_json()["total"] == 1
    assert client.get("/api/photos/assets?backed_up=true").get_json()["total"] == 1
    assert client.get("/api/photos/assets?search=IMG_3").get_json()["total"] == 1


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
    assert client.put("/api/photos/assets/still/mark", json={"mark": "delete"}).status_code == 200

    assert client.get("/api/photos/assets?mark=delete").get_json()["total"] == 1
    assert client.get("/api/photos/facets").get_json()["totals"]["marked"] == 1

    client.put("/api/photos/assets/still/mark", json={"mark": None})

    assert client.get("/api/photos/assets?mark=delete").get_json()["total"] == 0


@pytest.mark.parametrize("body", [{"mark": "burn"}, {}, []], ids=["unknown", "empty", "not_an_object"])
def test_a_mark_the_library_does_not_define_is_refused(client: FlaskClient, body: object) -> None:
    assert client.put("/api/photos/assets/still/mark", json=body).status_code == 400


def test_marking_an_asset_that_does_not_exist_is_a_404(client: FlaskClient) -> None:
    assert client.put("/api/photos/assets/never-existed/mark", json={"mark": "delete"}).status_code == 404


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
    home = client.get("/")

    assert page.status_code == 200
    assert b"gallery.js" in page.data
    assert b'href="/photos"' in home.data
