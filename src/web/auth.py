"""The shared secret that gates every write route, whichever feature owns it.

Unset (the default) leaves writes open, which is fine for the localhost
interface `mycloud serve` binds by default. Set API_WRITE_TOKEN in .env before
exposing the app on a network or through a tunnel.
"""

from flask import abort
from flask import request

from src.core.env import API_WRITE_TOKEN


def require_write_token() -> None:
    """Abort 401 unless the request carries the configured token, if any."""
    if not API_WRITE_TOKEN:
        return
    if request.headers.get("X-Api-Token") != API_WRITE_TOKEN:
        abort(401, description="Missing or invalid X-Api-Token header.")
