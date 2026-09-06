"""The photo gallery page, plus the blueprint that owns its assets.

A thin client, like the Find My page: it renders empty markup and calls the
JSON routes in api.py from the browser.
"""

from flask import Blueprint
from flask import render_template

bp = Blueprint(
    "photos",
    __name__,
    url_prefix="/photos",
    template_folder="templates",
    static_folder="static",
)


@bp.get("")
def page() -> str:
    return render_template("photos/gallery.html", active_page="photos")
