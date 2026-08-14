"""Mostly-read-only Flask API over the SQLite data the background poller maintains.

Most routes only read `src.db` -- no request ever waits on a live Apple
fetch, which is the whole point of polling in the background instead of
fetching on demand. The exceptions are `PUT /locations/<id>/icon`, which
stores a user-chosen marker emoji, and `POST /alerts` / `DELETE
/alerts/<id>`, which manage user-configured movement/enter/exit alerts
(evaluated by src/alerts.py from the poller, not from a request). See
src/poller.py for how the data gets there and `uv run findmy serve --help`
for how to run this.

`/dashboard` serves a small HTML page (src/templates/dashboard.html,
src/static/dashboard.{css,js}) that plots device tracks on a Leaflet/OpenStreetMap
map by calling the JSON routes below from the browser -- it's a thin client,
not a server-rendered view, so it has no server-side state of its own.
"""

import atexit
import math
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
from src.db import create_alert
from src.db import get_alert
from src.db import history_for
from src.db import init_db
from src.db import last_updated
from src.db import latest_location_for
from src.db import list_alerts
from src.db import remove_alert
from src.db import set_device_icon
from src.env import API_WRITE_TOKEN
from src.env import TELEGRAM_API_TOKEN
from src.env import TELEGRAM_CHAT_ID
from src.poller import start_background_poller
from src.telemetry import logger as _telemetry_logger  # noqa: F401  (wires stdout + Spyglass logging)
from src.tracking import distance_from_home_m_at

_VALID_ALERT_TYPES = {"movement", "enter", "exit"}

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


def _serialize_alert(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "device_name": row["device_name"],
        "device_icon": row["device_icon"],
        "alert_type": row["alert_type"],
        "threshold_m": row["threshold_m"],
        "created_at": row["created_at"],
        "is_active": bool(row["is_active"]),
        "triggered_at": row["triggered_at"],
        "anchor_lat": row["anchor_lat"],
        "anchor_lon": row["anchor_lon"],
    }


_VALID_ANCHORS = {"home", "current"}


def _parse_alert_payload() -> tuple[str, str, float, str]:
    """Return (device_id, alert_type, threshold_m, anchor) from the request body, aborting 400 on junk.

    `anchor` is only meaningful for `enter`/`exit` alerts -- `"home"` (the
    default) measures from the configured home coordinates, `"current"` tells
    the caller (post_alert) to snapshot the device's current location as a
    fixed anchor point instead. Ignored for `movement` alerts.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(
            400, description="Body must be a JSON object with 'device_id', 'alert_type', and 'threshold_m'."
        )

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        abort(400, description="'device_id' must be a non-empty string.")

    alert_type = payload.get("alert_type")
    if alert_type not in _VALID_ALERT_TYPES:
        abort(400, description=f"'alert_type' must be one of: {', '.join(sorted(_VALID_ALERT_TYPES))}.")

    threshold_m = payload.get("threshold_m")
    if isinstance(threshold_m, bool) or not isinstance(threshold_m, (int, float)):
        abort(400, description="'threshold_m' must be a number.")
    if not math.isfinite(threshold_m) or threshold_m <= 0:
        abort(400, description="'threshold_m' must be a finite number greater than 0.")

    anchor = payload.get("anchor", "home")
    if anchor not in _VALID_ANCHORS:
        abort(400, description=f"'anchor' must be one of: {', '.join(sorted(_VALID_ANCHORS))}.")

    return device_id, alert_type, float(threshold_m), anchor


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
        return jsonify(
            {
                "home_latitude": HOME_LATITUDE,
                "home_longitude": HOME_LONGITUDE,
                "telegram_configured": bool(TELEGRAM_API_TOKEN and TELEGRAM_CHAT_ID),
            }
        )

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

    @app.get("/alerts")
    def get_alerts() -> ResponseReturnValue:
        with connection() as conn:
            rows = list_alerts(conn)
        return jsonify([_serialize_alert(row) for row in rows])

    @app.post("/alerts")
    def post_alert() -> ResponseReturnValue:
        _require_write_token()
        device_id, alert_type, threshold_m, anchor = _parse_alert_payload()
        anchor_lat = anchor_lon = None
        with connection() as conn:
            if anchor == "current":
                location = latest_location_for(conn, device_id)
                if location is not None and location["latitude"] is not None:
                    anchor_lat, anchor_lon = location["latitude"], location["longitude"]
                elif location is not None:
                    abort(400, description="Cannot anchor to current location: device has no fix yet.")
                # else: unknown device_id -- create_alert below 404s.

            alert_id = create_alert(
                conn, device_id, alert_type, threshold_m, anchor_lat=anchor_lat, anchor_lon=anchor_lon
            )
            if alert_id is None:
                abort(404, description=f"Unknown device_id: {device_id!r}.")
            row = get_alert(conn, alert_id)
            assert row is not None
        return jsonify(_serialize_alert(row)), 201

    @app.delete("/alerts/<int:alert_id>")
    def delete_alert(alert_id: int) -> ResponseReturnValue:
        _require_write_token()
        with connection() as conn:
            if not remove_alert(conn, alert_id):
                abort(404)
        return "", 204

    return app
