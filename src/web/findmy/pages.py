"""The Find My page itself, plus the blueprint that owns its assets.

A thin client: it renders empty markup and calls the JSON routes in api.py
from the browser, so there is no server-side view state here. Its assets are
served under /findmy/static/, where they can't collide with another page's.
"""

from flask import Blueprint
from flask import render_template

bp = Blueprint(
    "findmy",
    __name__,
    url_prefix="/findmy",
    template_folder="templates",
    static_folder="static",
)


@bp.get("")
def page() -> str:
    return render_template("findmy/dashboard.html", active_page="findmy")
