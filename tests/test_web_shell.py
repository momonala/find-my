"""Tests for the shell: the landing page, the nav, and the shared routes.

Everything here is what a page gets for free by extending base.html --
nothing in it should know about Find My beyond its entry in src/web/nav.py.
"""

import re

import src.web.app as app_module
import src.web.tiles as tiles
from src.web.nav import NAV_ITEMS


def _current_nav_link(body: str) -> str | None:
    """The href of the nav link marked `aria-current`, or None if none is."""
    match = re.search(r'href="([^"]+)"[^>]*aria-current="page"', body)
    return match.group(1) if match else None


def test_landing_page_lists_every_nav_entry(client):
    body = client.get("/").data.decode()
    for item in NAV_ITEMS:
        assert item.label in body
        assert item.summary in body


def test_landing_page_marks_itself_as_the_current_page(client):
    """`aria-current` is what tells a screen reader which page this is."""
    assert _current_nav_link(client.get("/").data.decode()) == "/"


def test_a_feature_page_marks_its_own_nav_entry_instead(client):
    assert _current_nav_link(client.get("/findmy").data.decode()) == "/findmy"


def test_the_old_dashboard_url_still_reaches_the_page(client):
    """It was the only URL before the shell existed, so bookmarks point at it."""
    response = client.get("/dashboard")
    assert response.status_code == 301
    assert response.headers["Location"] == "/findmy"


def test_shared_assets_are_served(client):
    for filename in (
        "tokens.css",
        "shell.css",
        "banners.js",
        "dialogs.js",
        "format.js",
        "http.js",
        "motion.js",
    ):
        assert client.get(f"/static/{filename}").status_code == 200, filename


def test_config_exposes_the_basemap_keys(client, monkeypatch):
    """Map keys are shell-wide: any page that draws a map reads them from here."""
    monkeypatch.setattr(app_module, "MAPTILER_API_KEY", "")
    monkeypatch.setattr(app_module, "offered_map_types", lambda: [])
    assert client.get("/api/config").get_json() == {"maptiler_key": "", "google_map_types": []}


def test_google_tile_route_proxies_bytes(client, monkeypatch):
    monkeypatch.setattr(tiles, "fetch_tile", lambda *_: (b"PNG", "image/png"))
    response = client.get("/api/tiles/google/roadmap/5/1/2")
    assert response.status_code == 200
    assert response.data == b"PNG"
    assert response.headers["Cache-Control"] == "public, max-age=2592000, immutable"


def test_google_tile_route_passes_through_the_upstream_content_type(client, monkeypatch):
    monkeypatch.setattr(tiles, "fetch_tile", lambda *_: (b"JPG", "image/jpeg"))
    assert client.get("/api/tiles/google/hybrid/5/1/2").headers["Content-Type"] == "image/jpeg"


def test_google_tile_route_rejects_an_unknown_map_type(client, monkeypatch):
    def _unknown(*_):
        raise tiles.UnknownMapType("nope")

    monkeypatch.setattr(tiles, "fetch_tile", _unknown)
    assert client.get("/api/tiles/google/streetview/5/1/2").status_code == 404


def test_google_tile_route_reports_upstream_failure(client, monkeypatch):
    def _fail(*_):
        raise tiles.GoogleTilesError("boom")

    monkeypatch.setattr(tiles, "fetch_tile", _fail)
    assert client.get("/api/tiles/google/roadmap/5/1/2").status_code == 502


def test_config_lists_the_google_map_types_the_server_offers(client, monkeypatch):
    monkeypatch.setattr(
        app_module, "offered_map_types", lambda: [{"type": "roadmap", "label": "Google Roadmap"}]
    )
    assert client.get("/api/config").get_json()["google_map_types"] == [
        {"type": "roadmap", "label": "Google Roadmap"}
    ]
