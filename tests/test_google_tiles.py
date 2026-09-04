"""Tests for src/maps/google_tiles.py's session-token caching and tile fetch."""

import threading
import time
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from joblib import Memory

from src.maps import google_tiles
from src.maps.google_tiles import GoogleTilesError
from src.maps.google_tiles import UnknownMapType
from src.maps.google_tiles import fetch_tile
from src.maps.google_tiles import session_token


@pytest.fixture(autouse=True)
def _isolated_caches(tmp_path):
    """Give each test its own session cache and tile cache directory."""
    google_tiles._sessions.clear()
    memory = Memory(tmp_path / "tiles", verbose=0)
    with patch.object(google_tiles, "_cached_download", memory.cache(google_tiles._download_tile)):
        yield
    google_tiles._sessions.clear()


def _session_response(token: str = "sess-1", *, expires_in: float = 3600.0) -> MagicMock:
    payload = {"session": token, "expiry": str(time.time() + expires_in)}
    return MagicMock(ok=True, json=MagicMock(return_value=payload))


def _tile_response(status: int, body: bytes = b"", content_type: str = "image/png") -> MagicMock:
    return MagicMock(
        ok=status == 200, status_code=status, content=body, headers={"Content-Type": content_type}, text=""
    )


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.post")
def test_session_token_is_minted_once_and_reused(mock_post):
    mock_post.return_value = _session_response()
    assert session_token("roadmap") == "sess-1"
    assert session_token("roadmap") == "sess-1"
    assert mock_post.call_count == 1
    assert mock_post.call_args.kwargs["json"]["mapType"] == "roadmap"


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.post")
def test_each_map_type_gets_its_own_session(mock_post):
    mock_post.side_effect = [_session_response("road"), _session_response("sat")]
    assert session_token("roadmap") == "road"
    assert session_token("satellite") == "sat"
    assert session_token("roadmap") == "road"


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.post")
def test_session_token_is_reminted_once_near_expiry(mock_post):
    # Inside _EXPIRY_MARGIN_S of expiry, so still valid to Google but due here.
    mock_post.side_effect = [_session_response("old", expires_in=60), _session_response("new")]
    assert session_token("roadmap") == "old"
    assert session_token("roadmap") == "new"


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.get")
@patch("src.maps.google_tiles.requests.post")
def test_a_cached_tile_is_not_downloaded_twice(mock_post, mock_get):
    mock_post.return_value = _session_response()
    mock_get.return_value = _tile_response(200, b"PNG")
    assert fetch_tile("roadmap", 5, 1, 2) == (b"PNG", "image/png")
    assert fetch_tile("roadmap", 5, 1, 2) == (b"PNG", "image/png")
    assert mock_get.call_count == 1


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.get")
@patch("src.maps.google_tiles.requests.post")
def test_each_coordinate_is_cached_separately(mock_post, mock_get):
    mock_post.return_value = _session_response()
    mock_get.side_effect = [_tile_response(200, b"one"), _tile_response(200, b"two")]
    assert fetch_tile("roadmap", 5, 1, 2)[0] == b"one"
    assert fetch_tile("roadmap", 5, 1, 3)[0] == b"two"


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.get")
@patch("src.maps.google_tiles.requests.post")
def test_a_failed_tile_is_not_cached(mock_post, mock_get):
    mock_post.return_value = _session_response()
    mock_get.side_effect = [_tile_response(500), _tile_response(200, b"PNG")]
    with pytest.raises(GoogleTilesError):
        fetch_tile("roadmap", 5, 1, 2)
    assert fetch_tile("roadmap", 5, 1, 2)[0] == b"PNG"


def test_offered_map_types_is_empty_without_a_key():
    with patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", ""):
        assert google_tiles.offered_map_types() == []


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
def test_offered_map_types_labels_every_type_it_can_serve():
    offered = google_tiles.offered_map_types()
    assert [entry["type"] for entry in offered] == list(google_tiles._MAP_TYPES)
    assert all(entry["label"] for entry in offered)


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.post")
def test_concurrent_callers_mint_one_session_between_them(mock_post):
    def _slow_mint(*_args, **_kwargs):
        time.sleep(0.05)
        return _session_response()

    mock_post.side_effect = _slow_mint
    tokens: list[str] = []
    threads = [threading.Thread(target=lambda: tokens.append(session_token("roadmap"))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert tokens == ["sess-1"] * 8
    assert mock_post.call_count == 1


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
def test_unknown_map_type_is_rejected():
    with pytest.raises(UnknownMapType, match="Unknown Google map type"):
        session_token("streetview")


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "")
def test_session_token_requires_a_configured_key():
    with pytest.raises(GoogleTilesError, match="not set"):
        session_token("roadmap")


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.post")
def test_malformed_session_payload_raises_a_google_tiles_error(mock_post):
    mock_post.return_value = MagicMock(ok=True, json=MagicMock(return_value={"sessionToken": "x"}))
    with pytest.raises(GoogleTilesError, match="unusable payload"):
        session_token("roadmap")


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.get")
@patch("src.maps.google_tiles.requests.post")
def test_fetch_tile_returns_image_bytes(mock_post, mock_get):
    mock_post.return_value = _session_response()
    mock_get.return_value = _tile_response(200, b"PNG")
    assert fetch_tile("hybrid", 5, 1, 2) == (b"PNG", "image/png")
    assert mock_get.call_args.kwargs["params"] == {"session": "sess-1", "key": "key-1"}


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.get")
@patch("src.maps.google_tiles.requests.post")
def test_fetch_tile_reports_the_upstream_content_type(mock_post, mock_get):
    mock_post.return_value = _session_response()
    mock_get.return_value = _tile_response(200, b"JPG", content_type="image/jpeg")
    assert fetch_tile("satellite", 5, 1, 2)[1] == "image/jpeg"


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.get")
@patch("src.maps.google_tiles.requests.post")
def test_rejected_session_is_reminted_and_the_tile_retried(mock_post, mock_get):
    mock_post.side_effect = [_session_response("stale"), _session_response("fresh")]
    mock_get.side_effect = [_tile_response(401), _tile_response(200, b"PNG")]
    assert fetch_tile("roadmap", 5, 1, 2)[0] == b"PNG"
    assert mock_get.call_args_list[1].kwargs["params"]["session"] == "fresh"


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.get")
@patch("src.maps.google_tiles.requests.post")
def test_fetch_tile_gives_up_after_one_remint(mock_post, mock_get):
    mock_post.side_effect = [_session_response("stale"), _session_response("fresh")]
    mock_get.return_value = _tile_response(403)
    with pytest.raises(GoogleTilesError, match="403"):
        fetch_tile("roadmap", 5, 1, 2)
    assert mock_get.call_count == 2


@patch("src.maps.google_tiles.GOOGLE_MAPS_API_KEY", "key-1")
@patch("src.maps.google_tiles.requests.get")
@patch("src.maps.google_tiles.requests.post")
def test_non_auth_failure_is_not_retried(mock_post, mock_get):
    mock_post.return_value = _session_response()
    mock_get.return_value = _tile_response(500)
    with pytest.raises(GoogleTilesError, match="500"):
        fetch_tile("roadmap", 5, 1, 2)
    assert mock_get.call_count == 1
