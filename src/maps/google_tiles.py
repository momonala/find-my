"""Google Map Tiles API: session tokens, a disk-cached raster tile fetch.

Optional -- GOOGLE_MAPS_API_KEY is blank by default, in which case
`offered_map_types()` is empty and the dashboard's style picker doesn't offer
the Google styles at all.

Unlike the CARTO/MapTiler basemaps, Google's 2D tiles are not a plain
{z}/{x}/{y} URL the browser can hit directly: each request needs a *session
token* minted by POST /v1/createSession, and every tile is billed against a
100k/month free allowance. So the key stays server-side, src/web/app.py proxies
tiles through GET /tiles/google/<map_type>/<z>/<x>/<y>, and downloads are cached
on disk -- a dashboard centred on one home area converges on a few hundred
tiles, so cache hits are what keep it free across browsers and restarts.

This module owns which map types exist; the dashboard renders whatever
`offered_map_types()` reports rather than keeping its own list.

Every failure leaves here as a GoogleTilesError.
"""

import logging
import threading
import time
from datetime import timedelta
from typing import Any

import requests
from joblib import Memory
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from src.core.env import GOOGLE_MAPS_API_KEY
from src.core.paths import DATA_DIR
from src.core.telemetry import metrics

logger = logging.getLogger(__name__)

_BASE_URL = "https://tile.googleapis.com/v1"
_REQUEST_TIMEOUT_S = 10

# Statuses Google returns for a session token it no longer accepts, which it can
# do before the token's stated expiry.
_SESSION_REJECTED = (401, 403)

# Renew a little before the stated expiry rather than discovering it mid-pan.
_EXPIRY_MARGIN_S = 300

_CACHE_DIR = DATA_DIR / "google_tiles"
_CACHE_MAX_AGE = timedelta(days=30)


class GoogleTilesError(RuntimeError):
    """A Google Map Tiles API request failed."""


class UnknownMapType(GoogleTilesError):
    """The requested map type isn't one this module offers."""


class _MapType(BaseModel):
    """One offered map type: its picker label and its createSession body."""

    model_config = ConfigDict(frozen=True)

    label: str
    session_body: dict[str, Any]


# "hybrid" isn't a Google map type -- it's the satellite imagery with the
# roadmap layer composited on top, which is the labelled satellite view people
# actually mean when they ask for satellite.
_MAP_TYPES: dict[str, _MapType] = {
    "roadmap": _MapType(label="Google Roadmap", session_body={"mapType": "roadmap"}),
    "hybrid": _MapType(
        label="Google Satellite",
        session_body={"mapType": "satellite", "layerTypes": ["layerRoadmap"]},
    ),
    "satellite": _MapType(label="Google Satellite (no labels)", session_body={"mapType": "satellite"}),
    "terrain": _MapType(
        label="Google Terrain",
        session_body={"mapType": "terrain", "layerTypes": ["layerRoadmap"]},
    ),
}


class _Session(BaseModel):
    """A createSession response, named for what the code needs rather than the wire."""

    model_config = ConfigDict(frozen=True)

    token: str = Field(alias="session")
    expires_at: float = Field(alias="expiry")


_sessions: dict[str, _Session] = {}
# One lock per map type rather than one global: minting is a blocking HTTP call,
# and a cold roadmap session must not stall tiles for an already-warm satellite
# one. The map types are fixed, so the locks can be built up front.
_session_locks = {map_type: threading.Lock() for map_type in _MAP_TYPES}


def is_configured() -> bool:
    return bool(GOOGLE_MAPS_API_KEY)


def offered_map_types() -> list[dict[str, str]]:
    """The map types the dashboard may offer, as `{"type", "label"}` entries."""
    if not is_configured():
        return []
    return [{"type": name, "label": spec.label} for name, spec in _MAP_TYPES.items()]


def prune_tile_cache() -> None:
    """Drop cached tiles older than `_CACHE_MAX_AGE` so stale imagery expires."""
    _memory.reduce_size(age_limit=_CACHE_MAX_AGE)


def _mint_session(map_type: str) -> _Session:
    """Create a new session token for `map_type`."""
    body = {"language": "en-US", "region": "US", **_MAP_TYPES[map_type].session_body}
    response = requests.post(
        f"{_BASE_URL}/createSession",
        params={"key": GOOGLE_MAPS_API_KEY},
        json=body,
        timeout=_REQUEST_TIMEOUT_S,
    )
    if not response.ok:
        metrics.increment("google_tiles_session_failed")
        raise GoogleTilesError(
            f"createSession for {map_type} failed ({response.status_code}): {response.text}"
        )

    try:
        session = _Session.model_validate(response.json())
    except (ValidationError, ValueError) as error:
        metrics.increment("google_tiles_session_failed")
        raise GoogleTilesError(f"createSession for {map_type} returned an unusable payload") from error

    metrics.increment("google_tiles_session_created")
    logger.info("Minted Google tile session for %s", map_type)
    return session


def session_token(map_type: str) -> str:
    """Return a cached session token for `map_type`, minting one if needed.

    Raises:
        UnknownMapType: `map_type` isn't offered.
        GoogleTilesError: no API key is configured, or minting failed.
    """
    if map_type not in _MAP_TYPES:
        raise UnknownMapType(f"Unknown Google map type: {map_type}")
    if not is_configured():
        raise GoogleTilesError("GOOGLE_MAPS_API_KEY is not set")

    with _session_locks[map_type]:
        cached = _sessions.get(map_type)
        if cached is not None and not _is_due(cached):
            return cached.token
        session = _mint_session(map_type)
        _sessions[map_type] = session
        return session.token


def _discard_session(map_type: str) -> None:
    _sessions.pop(map_type, None)


def _is_due(session: _Session) -> bool:
    return session.expires_at - _EXPIRY_MARGIN_S <= time.time()


def _request_tile(token: str, z: int, x: int, y: int) -> requests.Response:
    return requests.get(
        f"{_BASE_URL}/2dtiles/{z}/{x}/{y}",
        params={"session": token, "key": GOOGLE_MAPS_API_KEY},
        timeout=_REQUEST_TIMEOUT_S,
    )


def _download_tile(map_type: str, z: int, x: int, y: int) -> tuple[bytes, str]:
    """Fetch one tile from Google, re-minting once if the session is rejected.

    Raises:
        UnknownMapType: `map_type` isn't offered.
        GoogleTilesError: the tile couldn't be fetched.
    """
    token = session_token(map_type)
    response = _request_tile(token, z, x, y)

    if response.status_code in _SESSION_REJECTED:
        logger.warning("Google tile session for %s rejected (%s); re-minting", map_type, response.status_code)
        _discard_session(map_type)
        response = _request_tile(session_token(map_type), z, x, y)

    if not response.ok:
        metrics.increment("google_tiles_failed")
        raise GoogleTilesError(
            f"Tile {map_type}/{z}/{x}/{y} failed ({response.status_code}): {response.text}"
        )

    metrics.increment("google_tiles_downloaded")
    return response.content, response.headers.get("Content-Type", "image/png")


# A `None` location disables caching and creates nothing on disk, so an
# unconfigured deployment doesn't grow a cache directory it can never fill.
_memory = Memory(_CACHE_DIR if is_configured() else None, verbose=0)
# joblib caches returns, not exceptions, so a failed tile is retried next time.
_cached_download = _memory.cache(_download_tile)


def fetch_tile(map_type: str, z: int, x: int, y: int) -> tuple[bytes, str]:
    """Return one raster tile as (image bytes, content type), cached on disk.

    `google_tiles_downloaded` counts real Google requests and
    `google_tiles_served` counts responses, so the gap between them is the
    cache hit rate.

    Raises:
        UnknownMapType: `map_type` isn't offered.
        GoogleTilesError: the tile couldn't be fetched.
    """
    image, content_type = _cached_download(map_type, z, x, y)
    metrics.increment("google_tiles_served")
    return image, content_type
