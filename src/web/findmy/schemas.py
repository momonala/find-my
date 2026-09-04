"""Request parsing and response shaping for the Find My JSON routes.

Kept apart from the routes in api.py so the routes read as routing: each one
is a query plus one of these. Everything here either returns a plain dict or
aborts 400 -- no route decides what a bad payload means.
"""

import math
import sqlite3
from typing import Any

from flask import abort
from flask import request

from src.findmy.tracking import distance_from_home_m_at

VALID_ALERT_TYPES = {"movement", "enter", "exit"}
VALID_ANCHORS = {"home", "current"}

# Sensible fallback emoji for common Apple device kinds (src/findmy/devices.py's
# `device.device_type` values), used until a user sets their own via
# PUT /locations/<id>/icon. Trackers/items have no such lookup -- their kind
# is user-defined hardware, not a fixed Apple product line.
DEFAULT_ICONS = {
    "iPhone": "📱",
    "iPad": "📱",
    "MacBookPro": "💻",
    "MacBookAir": "💻",
}

# Long enough for a flag sequence or an emoji with a skin-tone modifier, short
# enough that the column can't be repurposed as arbitrary storage. Mirrored by
# ICON_MAX_LENGTH in src/web/findmy/static/dashboard.js.
MAX_ICON_LENGTH = 16

_EMOJI_BODY_SHAPE = "an 'emoji' key (null to clear)"


def serialize_location(row: sqlite3.Row) -> dict[str, Any]:
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
        "icon": row["icon"] or DEFAULT_ICONS.get(row["kind"]),
        "battery_level": row["battery_level"],
        "latitude": latitude,
        "longitude": longitude,
        "seen_at": row["seen_at"],
        "distance_m": round(distance_from_home_m_at(latitude, longitude)) if has_fix else None,
    }


def serialize_fix(row: sqlite3.Row) -> dict[str, Any]:
    return {"latitude": row["latitude"], "longitude": row["longitude"], "seen_at": row["seen_at"]}


def serialize_alert(row: sqlite3.Row) -> dict[str, Any]:
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


def _json_object_body(expected: str) -> dict[str, Any]:
    """The request body as a JSON object, aborting 400 with `expected` if it isn't one."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description=f"Body must be a JSON object with {expected}.")
    return payload


def parse_icon_payload() -> str | None:
    """Return the requested emoji, or None to clear it, aborting 400 on junk.

    Without a cap this column is arbitrary user-controlled storage that every
    dashboard visitor then renders, so the shape is checked rather than trusted.
    """
    payload = _json_object_body(_EMOJI_BODY_SHAPE)
    if "emoji" not in payload:
        abort(400, description=f"Body must be a JSON object with {_EMOJI_BODY_SHAPE}.")

    emoji = payload["emoji"]
    if emoji is None:
        return None
    if not isinstance(emoji, str):
        abort(400, description="'emoji' must be a string or null.")

    emoji = emoji.strip()
    if not emoji:
        return None
    if len(emoji) > MAX_ICON_LENGTH:
        abort(400, description=f"'emoji' must be at most {MAX_ICON_LENGTH} characters.")
    if any(not character.isprintable() for character in emoji):
        abort(400, description="'emoji' must not contain control characters.")
    return emoji


def _parse_alert_fields(payload: dict[str, Any]) -> tuple[str, float, str]:
    """Shared alert_type/threshold_m/anchor validation for create and update payloads, aborting 400 on junk.

    `anchor` is only meaningful for `enter`/`exit` alerts -- `"home"` (the
    default) measures from the configured home coordinates, `"current"` tells
    the caller to snapshot the device's current location as a fixed anchor
    point instead. Ignored for `movement` alerts.
    """
    alert_type = payload.get("alert_type")
    if alert_type not in VALID_ALERT_TYPES:
        abort(400, description=f"'alert_type' must be one of: {', '.join(sorted(VALID_ALERT_TYPES))}.")

    threshold_m = payload.get("threshold_m")
    if isinstance(threshold_m, bool) or not isinstance(threshold_m, int | float):
        abort(400, description="'threshold_m' must be a number.")
    if not math.isfinite(threshold_m) or threshold_m <= 0:
        abort(400, description="'threshold_m' must be a finite number greater than 0.")

    anchor = payload.get("anchor", "home")
    if anchor not in VALID_ANCHORS:
        abort(400, description=f"'anchor' must be one of: {', '.join(sorted(VALID_ANCHORS))}.")

    return alert_type, float(threshold_m), anchor


def parse_alert_payload() -> tuple[str, str, float, str]:
    """Return (device_id, alert_type, threshold_m, anchor) from the request body, aborting 400 on junk."""
    payload = _json_object_body("'device_id', 'alert_type', and 'threshold_m'")

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        abort(400, description="'device_id' must be a non-empty string.")

    alert_type, threshold_m, anchor = _parse_alert_fields(payload)
    return device_id, alert_type, threshold_m, anchor


def parse_alert_update_payload() -> tuple[str, float, str]:
    """Return (alert_type, threshold_m, anchor) from the request body, aborting 400 on junk.

    No `device_id` here -- an alert's device is fixed at creation, so editing
    only ever touches type/threshold/anchor.
    """
    return _parse_alert_fields(_json_object_body("'alert_type' and 'threshold_m'"))
