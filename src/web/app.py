"""The Flask app: a shared shell plus one blueprint per feature.

Feature routes live with their feature (src/web/findmy/), not here. This
module owns only what every page shares -- the landing page, the nav-bar
context, the map keys in /api/config -- and the boot wiring: schema
migrations, the tile cache prune, and the background poller.
"""

import atexit

from flask import Flask
from flask import jsonify
from flask import redirect
from flask import render_template
from flask import url_for
from flask.typing import ResponseReturnValue

import src.core.telemetry  # noqa: F401  -- imported for its side effect: wires stdout + Spyglass logging
from src.core.db import init_db
from src.core.env import MAPTILER_API_KEY
from src.findmy.poller import start_background_poller
from src.maps.google_tiles import is_configured as google_tiles_configured
from src.maps.google_tiles import offered_map_types
from src.maps.google_tiles import prune_tile_cache
from src.web import tiles
from src.web.findmy import api as findmy_api
from src.web.findmy import pages as findmy_pages
from src.web.nav import NAV_ITEMS


def create_app(*, start_poller: bool = True) -> Flask:
    """Build the Flask app, wiring up the DB schema and (optionally) the poller.

    `start_poller=False` is for tests -- it lets them seed a temp DB directly and
    hit the routes without a background thread racing real network calls -- and
    for deployments that run `findmy poll` as its own process, so that exactly
    one process writes to the database.
    """
    init_db()
    if google_tiles_configured():
        prune_tile_cache()

    app = Flask(__name__)

    app.register_blueprint(tiles.bp)
    app.register_blueprint(findmy_pages.bp)
    app.register_blueprint(findmy_api.bp)

    if start_poller:
        stop_event = start_background_poller()
        app.extensions["poller_stop"] = stop_event
        # The thread is a daemon, so it dies with the process either way; this
        # makes an orderly shutdown end the current wait instead of abandoning it.
        atexit.register(stop_event.set)

    @app.context_processor
    def inject_nav() -> dict[str, object]:
        """Every template renders the same nav, so no view has to pass it."""
        return {"nav_items": NAV_ITEMS}

    @app.get("/")
    def home() -> str:
        return render_template("home.html", active_page="home")

    @app.get("/dashboard")
    def legacy_dashboard() -> ResponseReturnValue:
        """Where the dashboard used to live, kept for bookmarks and external links."""
        return redirect(url_for("findmy.page"), code=301)

    @app.get("/api/config")
    def get_config() -> ResponseReturnValue:
        """Settings any page with a map needs. Feature settings live under /api/<feature>/config."""
        return jsonify(
            {
                "maptiler_key": MAPTILER_API_KEY,
                "google_map_types": offered_map_types(),
            }
        )

    return app
