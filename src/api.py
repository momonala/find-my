"""Read-only Flask API over the SQLite data the background poller maintains.

Every route only reads `src.db` -- no request ever waits on a live Apple
fetch, which is the whole point of polling in the background instead of
fetching on demand. The one exception is `PUT /locations/<id>/icon`, which
stores a user-chosen marker emoji. See src/poller.py for how the data gets
there and `uv run findmy serve --help` for how to run this.

`/dashboard` serves a small HTML page (src/templates/dashboard.html,
src/static/dashboard.{css,js}) that plots device tracks on a Leaflet/OpenStreetMap
map by calling the JSON routes below from the browser -- it's a thin client,
not a server-rendered view, so it has no server-side state of its own.
"""

import atexit
import sqlite3
from typing import Any

from flask import Flask
from flask import abort
from flask import jsonify
from flask import redirect
from flask import render_template
from flask import request
from flask import url_for
from flask.typing import ResponseReturnValue

from src.config import HOME_LATITUDE
from src.config import HOME_LONGITUDE
from src.db import all_latest_locations
from src.db import connection
from src.db import history_for
from src.db import init_db
from src.db import last_updated
from src.db import latest_location_for
from src.db import set_device_icon
from src.env import API_WRITE_TOKEN
from src.poller import start_background_poller
from src.tracking import distance_from_home_m_at

# Sensible fallback emoji for common Apple device kinds (src/find_my.py's
# `device.device_type` values), used until a user sets their own via
# PUT /locations/<id>/icon. Trackers/items have no such lookup -- their kind
# is user-defined hardware, not a fixed Apple product line.
_DEFAULT_ICONS = {
    "iPhone": "📱",
    "iPad": "📱",
    "MacBookPro": "💻",
    "MacBookAir": "💻",
}

# Long enough for a flag sequence or an emoji with a skin-tone modifier, short
# enough that the column can't be repurposed as arbitrary storage.
_MAX_ICON_LENGTH = 16


def _serialize_location(row: sqlite3.Row) -> dict[str, Any]:
    """Shape a device row for the API, including its distance from home.

    `distance_m` is computed here rather than in the browser so that the CLI's
    `--json` output and this response share one haversine implementation.
    """
    latitude, longitude = row["latitude"], row["longitude"]
    has_fix = latitude is not None and longitude is not None
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "source": row["source"],
        "icon": row["icon"] or _DEFAULT_ICONS.get(row["kind"]),
        "latitude": latitude,
        "longitude": longitude,
        "seen_at": row["seen_at"],
        "distance_m": round(distance_from_home_m_at(latitude, longitude)) if has_fix else None,
    }


def _serialize_fix(row: sqlite3.Row) -> dict[str, Any]:
    return {"latitude": row["latitude"], "longitude": row["longitude"], "seen_at": row["seen_at"]}


def _parse_icon_payload() -> str | None:
    """Return the requested emoji, or None to clear it, aborting 400 on junk.

    Without a cap this column is arbitrary user-controlled storage that every
    dashboard visitor then renders, so the shape is checked rather than trusted.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or "emoji" not in payload:
        abort(400, description="Body must be a JSON object with an 'emoji' key (null to clear).")

    emoji = payload["emoji"]
    if emoji is None:
        return None
    if not isinstance(emoji, str):
        abort(400, description="'emoji' must be a string or null.")

    emoji = emoji.strip()
    if not emoji:
        return None
    if len(emoji) > _MAX_ICON_LENGTH:
        abort(400, description=f"'emoji' must be at most {_MAX_ICON_LENGTH} characters.")
    if any(not character.isprintable() for character in emoji):
        abort(400, description="'emoji' must not contain control characters.")
    return emoji


def _require_write_token() -> None:
    """Gate writes on a shared secret, when one is configured.

    Unset (the default) leaves writes open, which is fine for the localhost
    interface `findmy serve` binds by default. Set API_WRITE_TOKEN in .env
    before exposing the dashboard on a network or through a tunnel.
    """
    if not API_WRITE_TOKEN:
        return
    if request.headers.get("X-Api-Token") != API_WRITE_TOKEN:
        abort(401, description="Missing or invalid X-Api-Token header.")


def create_app(start_poller: bool = True) -> Flask:
    """Build the Flask app, wiring up the DB schema and (optionally) the poller.

    `start_poller=False` is for tests -- it lets them seed a temp DB directly and
    hit the routes without a background thread racing real network calls -- and
    for deployments that run `findmy poll` as its own process, so that exactly
    one process writes to the database.
    """
    init_db()

    app = Flask(__name__)

    if start_poller:
        stop_event = start_background_poller()
        app.extensions["poller_stop"] = stop_event
        # The thread is a daemon, so it dies with the process either way; this
        # makes an orderly shutdown end the current wait instead of abandoning it.
        atexit.register(stop_event.set)

    @app.get("/")
    def index() -> ResponseReturnValue:
        return redirect(url_for("dashboard"))

    @app.get("/dashboard")
    def dashboard() -> str:
        return render_template("dashboard.html")

    @app.get("/config")
    def get_config() -> ResponseReturnValue:
        return jsonify({"home_latitude": HOME_LATITUDE, "home_longitude": HOME_LONGITUDE})

    @app.get("/status")
    def get_status() -> ResponseReturnValue:
        with connection() as conn:
            updated_at = last_updated(conn)
        return jsonify({"last_updated": updated_at})

    @app.get("/locations")
    def list_locations() -> ResponseReturnValue:
        with connection() as conn:
            rows = all_latest_locations(conn)
        return jsonify([_serialize_location(row) for row in rows])

    @app.get("/locations/<path:device_id>")
    def get_location(device_id: str) -> ResponseReturnValue:
        with connection() as conn:
            row = latest_location_for(conn, device_id)
        if row is None:
            abort(404)
        return jsonify(_serialize_location(row))

    @app.put("/locations/<path:device_id>/icon")
    def put_icon(device_id: str) -> ResponseReturnValue:
        _require_write_token()
        emoji = _parse_icon_payload()
        with connection() as conn:
            if not set_device_icon(conn, device_id, emoji):
                abort(404)
            # Re-read so the response goes through the same serializer as GET
            # rather than echoing back the raw submitted value. Guaranteed
            # non-None: set_device_icon just confirmed the device exists, and
            # nothing else can delete it out from under this same connection.
            row = latest_location_for(conn, device_id)
            assert row is not None
        return jsonify(_serialize_location(row))

    @app.get("/locations/<path:device_id>/history")
    def get_history(device_id: str) -> ResponseReturnValue:
        since = request.args.get("since")
        limit = request.args.get("limit", type=int)
        with connection() as conn:
            rows = history_for(conn, device_id, since=since, limit=limit)
        if rows is None:
            abort(404)
        return jsonify([_serialize_fix(row) for row in rows])

    return app
