"""Request parsing every feature shares.

Anything only one feature needs belongs in that feature's own schemas module.
"""

from typing import Any

from flask import abort
from flask import request


def json_object_body(expected: str) -> dict[str, Any]:
    """The request body as a JSON object, aborting 400 with `expected` if it isn't one."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description=f"Body must be a JSON object with {expected}.")
    return payload
