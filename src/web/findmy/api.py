"""Mostly-read-only JSON routes over the SQLite data the poller maintains.

Nothing here waits on a live Apple fetch, which is the whole point of polling
in the background instead of fetching on demand -- see src/findmy/poller.py
for how the data gets there. The writes only ever touch SQLite: a marker
emoji, and the alert definitions that src/findmy/alerts.py evaluates from the
poller rather than from a request.
"""

from flask import Blueprint
from flask import abort
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue

from src.core.config import HOME_LATITUDE
from src.core.config import HOME_LONGITUDE
from src.core.db import connection
from src.core.env import TELEGRAM_API_TOKEN
from src.core.env import TELEGRAM_CHAT_ID
from src.findmy.db import all_latest_locations
from src.findmy.db import create_alert
from src.findmy.db import get_alert
from src.findmy.db import history_for
from src.findmy.db import last_updated
from src.findmy.db import latest_location_for
from src.findmy.db import list_alerts
from src.findmy.db import remove_alert
from src.findmy.db import set_device_icon
from src.findmy.db import update_alert
from src.web.auth import require_write_token
from src.web.findmy.schemas import parse_alert_payload
from src.web.findmy.schemas import parse_alert_update_payload
from src.web.findmy.schemas import parse_icon_payload
from src.web.findmy.schemas import serialize_alert
from src.web.findmy.schemas import serialize_fix
from src.web.findmy.schemas import serialize_location

bp = Blueprint("findmy_api", __name__, url_prefix="/api/findmy")


@bp.get("/config")
def get_config() -> ResponseReturnValue:
    """This feature's own settings. Map keys come from the shell's /api/config."""
    return jsonify(
        {
            "home_latitude": HOME_LATITUDE,
            "home_longitude": HOME_LONGITUDE,
            "telegram_configured": bool(TELEGRAM_API_TOKEN and TELEGRAM_CHAT_ID),
        }
    )


@bp.get("/status")
def get_status() -> ResponseReturnValue:
    with connection() as conn:
        updated_at = last_updated(conn)
    return jsonify({"last_updated": updated_at})


@bp.get("/locations")
def list_locations() -> ResponseReturnValue:
    with connection() as conn:
        rows = all_latest_locations(conn)
    return jsonify([serialize_location(row) for row in rows])


@bp.get("/locations/<path:device_id>")
def get_location(device_id: str) -> ResponseReturnValue:
    with connection() as conn:
        row = latest_location_for(conn, device_id)
    if row is None:
        abort(404)
    return jsonify(serialize_location(row))


@bp.put("/locations/<path:device_id>/icon")
def put_icon(device_id: str) -> ResponseReturnValue:
    require_write_token()
    emoji = parse_icon_payload()
    with connection() as conn:
        if not set_device_icon(conn, device_id, emoji):
            abort(404)
        # Re-read so the response goes through the same serializer as GET.
        # Non-None: set_device_icon just confirmed the device exists, and
        # nothing else can delete it out from under this same connection.
        row = latest_location_for(conn, device_id)
        assert row is not None
    return jsonify(serialize_location(row))


@bp.get("/locations/<path:device_id>/history")
def get_history(device_id: str) -> ResponseReturnValue:
    since = request.args.get("since")
    limit = request.args.get("limit", type=int)
    with connection() as conn:
        rows = history_for(conn, device_id, since=since, limit=limit)
    if rows is None:
        abort(404)
    return jsonify([serialize_fix(row) for row in rows])


@bp.get("/alerts")
def get_alerts() -> ResponseReturnValue:
    with connection() as conn:
        rows = list_alerts(conn)
    return jsonify([serialize_alert(row) for row in rows])


@bp.post("/alerts")
def post_alert() -> ResponseReturnValue:
    require_write_token()
    device_id, alert_type, threshold_m, anchor = parse_alert_payload()
    anchor_lat = anchor_lon = None
    with connection() as conn:
        if anchor == "current":
            location = latest_location_for(conn, device_id)
            if location is None:
                abort(404, description=f"Unknown device_id: {device_id!r}.")
            if location["latitude"] is None:
                abort(400, description="Cannot anchor to current location: device has no fix yet.")
            anchor_lat, anchor_lon = location["latitude"], location["longitude"]

        alert_id = create_alert(
            conn, device_id, alert_type, threshold_m, anchor_lat=anchor_lat, anchor_lon=anchor_lon
        )
        if alert_id is None:
            abort(404, description=f"Unknown device_id: {device_id!r}.")
        row = get_alert(conn, alert_id)
        assert row is not None
    return jsonify(serialize_alert(row)), 201


@bp.put("/alerts/<int:alert_id>")
def put_alert(alert_id: int) -> ResponseReturnValue:
    require_write_token()
    alert_type, threshold_m, anchor = parse_alert_update_payload()
    with connection() as conn:
        existing = get_alert(conn, alert_id)
        if existing is None:
            abort(404)

        anchor_lat = anchor_lon = None
        if anchor == "current":
            location = latest_location_for(conn, existing["device_id"])
            if location is None or location["latitude"] is None:
                abort(400, description="Cannot anchor to current location: device has no fix yet.")
            anchor_lat, anchor_lon = location["latitude"], location["longitude"]

        updated = update_alert(
            conn, alert_id, alert_type, threshold_m, anchor_lat=anchor_lat, anchor_lon=anchor_lon
        )
        if not updated:
            abort(404)
        row = get_alert(conn, alert_id)
        assert row is not None
    return jsonify(serialize_alert(row))


@bp.delete("/alerts/<int:alert_id>")
def delete_alert(alert_id: int) -> ResponseReturnValue:
    require_write_token()
    with connection() as conn:
        if not remove_alert(conn, alert_id):
            abort(404)
    return "", 204
