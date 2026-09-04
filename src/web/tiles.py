"""Basemap tile proxy, shared by any page that draws a map.

Thin by design: src/maps/google_tiles.py owns the session dance, the disk
cache and which map types exist. This is only the HTTP surface.
"""

import logging

from flask import Blueprint
from flask import abort
from flask import current_app
from flask import jsonify
from flask.typing import ResponseReturnValue

from src.maps.google_tiles import GoogleTilesError
from src.maps.google_tiles import UnknownMapType
from src.maps.google_tiles import fetch_tile

logger = logging.getLogger(__name__)

bp = Blueprint("tiles", __name__, url_prefix="/api/tiles")


@bp.get("/google/<map_type>/<int:z>/<int:x>/<int:y>")
def google_tile(map_type: str, z: int, x: int, y: int) -> ResponseReturnValue:
    """Proxy one Google Map Tiles raster tile."""
    try:
        image, content_type = fetch_tile(map_type, z, x, y)
    except UnknownMapType:
        abort(404)
    except GoogleTilesError as error:
        logger.warning("Google tile fetch failed: %s", error)
        return jsonify({"error": str(error)}), 502
    response = current_app.response_class(image, mimetype=content_type)
    # Basemap imagery is effectively static, so let the browser skip even the
    # proxy hop.
    response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return response
