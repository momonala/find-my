"""JSON and image routes over the photo library on the backup drive.

Read-only apart from three routes: the mark, which records a decision about an
asset; the refresh, which pulls from iCloud onto the drive; and the prune, which
carries the marks out and is the only thing here that deletes anything.

The library lives on removable storage, so every route opens its own connection
and fails with 503 rather than 500 when the drive is not mounted.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint
from flask import abort
from flask import jsonify
from flask import request
from flask import send_file
from flask.typing import ResponseReturnValue

from icloud_photo_metadata import library
from icloud_photo_metadata import queries
from icloud_photo_metadata.backup import DEFAULT_DIR
from src.web.auth import require_write_token
from src.web.photos.pruning import pruning
from src.web.photos.schemas import parse_filters
from src.web.photos.schemas import parse_mark_payload
from src.web.photos.schemas import parse_prune_payload
from src.web.photos.sync import refresh
from src.web.photos.thumbnails import SIZES
from src.web.photos.thumbnails import UnrenderableAsset
from src.web.photos.thumbnails import thumbnail

bp = Blueprint("photos_api", __name__, url_prefix="/api/photos")

_MAX_PAGE = 240


def _require_library() -> None:
    """An unplugged drive is a state to explain, not a server error."""
    if not library.exists(DEFAULT_DIR):
        abort(503, description=f"No photo library at {DEFAULT_DIR}. Is the backup drive mounted?")


@contextmanager
def _reading() -> Iterator[sqlite3.Connection]:
    """Every route but the mark, which is why reads never dirty the drive."""
    _require_library()
    with library.reading(DEFAULT_DIR) as conn:
        yield conn


@contextmanager
def _writing() -> Iterator[sqlite3.Connection]:
    _require_library()
    with library.library(DEFAULT_DIR) as conn:
        yield conn


@bp.get("/assets")
def list_assets() -> ResponseReturnValue:
    """One page of the gallery under the query string's filters."""
    filters = parse_filters()
    limit = min(request.args.get("limit", default=120, type=int), _MAX_PAGE)
    offset = max(request.args.get("offset", default=0, type=int), 0)
    with _reading() as conn:
        page = queries.search(conn, filters, limit=limit, offset=offset)
    return jsonify({"assets": page.rows, "total": page.total, "next_offset": page.next_offset})


@bp.get("/facets")
def get_facets() -> ResponseReturnValue:
    """The filter values this library actually holds, plus its headline totals."""
    with _reading() as conn:
        return jsonify({"facets": queries.facets(conn), "totals": queries.totals(conn)})


@bp.get("/assets/<asset_id>")
def get_asset(asset_id: str) -> ResponseReturnValue:
    with _reading() as conn:
        asset = queries.get_asset(conn, asset_id)
    if asset is None:
        abort(404, description=f"No asset {asset_id}.")
    return jsonify(asset)


@bp.put("/assets/<asset_id>/mark")
def put_mark(asset_id: str) -> ResponseReturnValue:
    """Flag an asset for deletion at one of `queries.MARKS`, or clear the flag.

    Recorded only. `POST /prune` is what carries it out.

    Deliberately does not answer with the library's totals. Recounting them
    means scanning every asset off the drive, which is 18 ms when its pages are
    cached and was measured at 3.7 s when they are not -- on the one route a
    person clicks over and over. The page knows the mark it sent and the one it
    replaced, which is the whole of what changed.
    """
    require_write_token()
    mark = parse_mark_payload()
    with _writing() as conn:
        if not queries.set_mark(conn, asset_id, mark):
            abort(404, description=f"No asset {asset_id}.")
    return jsonify({"id": asset_id, "mark": mark})


@bp.get("/refresh")
def get_refresh() -> ResponseReturnValue:
    """How the running -- or last -- sync is going."""
    return jsonify(refresh.state())


@bp.post("/refresh")
def post_refresh() -> ResponseReturnValue:
    """Pull what iCloud has that the drive does not, in the background.

    Answers as soon as the sync is under way; the run itself takes minutes, and
    GET reports where it got to.
    """
    require_write_token()
    _require_library()
    if not refresh.start(DEFAULT_DIR):
        abort(409, description="A refresh is already running.")
    return jsonify(refresh.state()), 202


@bp.get("/prune")
def get_prune() -> ResponseReturnValue:
    """How the running -- or last -- prune went."""
    return jsonify(pruning.state())


@bp.post("/prune")
def post_prune() -> ResponseReturnValue:
    """Delete what is marked, in the background.

    Answers as soon as the prune is under way. What it will delete is decided
    inside the run, from the marks as they stand then, so this route promises
    nothing about which assets it took.
    """
    require_write_token()
    _require_library()
    if not pruning.start(DEFAULT_DIR, parse_prune_payload()):
        abort(409, description="A prune is already running.")
    return jsonify(pruning.state()), 202


@bp.get("/assets/<asset_id>/image")
def get_image(asset_id: str) -> ResponseReturnValue:
    """A browser-renderable JPEG of the asset, at `size=grid` or `size=detail`.

    Videos have no still to serve: the client shows its own placeholder rather
    than this route inventing one.
    """
    size = request.args.get("size", default="grid")
    if size not in SIZES:
        abort(400, description=f"Unknown size {size!r}; expected one of {sorted(SIZES)}.")

    with _reading() as conn:
        relative_path = queries.original_path(conn, asset_id)
    if relative_path is None:
        abort(404, description=f"No backed-up original for {asset_id}.")

    try:
        rendered = thumbnail(_safe_original(relative_path), size)
    except FileNotFoundError:
        abort(404, description="The library claims a file the drive does not have.")
    except UnrenderableAsset:
        abort(415, description="That original is not an image this can render.")
    # A backed-up original never changes, and a changed one is a new asset id,
    # so a revalidation could only ever confirm what the browser already has.
    response = send_file(rendered, mimetype="image/jpeg", max_age=31536000)
    response.headers["Cache-Control"] += ", immutable"
    return response


def _safe_original(relative_path: str) -> Path:
    """Resolve a library path against the drive, refusing anything outside it.

    The path comes from the database rather than the request, but a route that
    turns text into a filesystem read is worth closing off regardless.
    """
    resolved = (DEFAULT_DIR / relative_path).resolve()
    if not resolved.is_relative_to(DEFAULT_DIR.resolve()):
        abort(404, description="That path is not on the backup drive.")
    return resolved
