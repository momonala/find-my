"""Request parsing for the photo routes.

Kept apart from api.py so the routes read as routing. Everything here either
returns a value the query layer accepts or aborts 400 -- no route decides what
a bad query string means.
"""

from __future__ import annotations

from flask import abort
from flask import request

from icloud_photo_metadata.queries import MARK_FILTERS
from icloud_photo_metadata.queries import MARKS
from icloud_photo_metadata.queries import SORTS
from icloud_photo_metadata.queries import Filters
from src.web.schemas import json_object_body

_MARK_BODY_SHAPE = "a 'mark' key of " + ", ".join(repr(mark) for mark in MARKS) + ", or null to clear"


def parse_filters() -> Filters:
    """Build the gallery's filters from the query string.

    An absent parameter is not a filter, so the unfiltered gallery and a
    filtered one are the same request with fewer keys.
    """
    sort = request.args.get("sort", default="newest")
    if sort not in SORTS:
        abort(400, description=f"Unknown sort {sort!r}; expected one of {sorted(SORTS)}.")

    mark = request.args.get("mark")
    if mark is not None and mark not in MARK_FILTERS:
        abort(400, description=f"Unknown mark {mark!r}; expected one of {list(MARK_FILTERS)}.")

    return Filters(
        album=request.args.get("album") or None,
        item_type=request.args.get("type") or None,
        camera_model=request.args.get("camera") or None,
        year=_int("year"),
        in_icloud=_flag("in_icloud"),
        backed_up=_flag("backed_up"),
        is_favorite=_flag("favorite"),
        has_location=_flag("located"),
        mark=mark,
        min_bytes=_int("min_bytes"),
        min_duration=_number("min_duration"),
        max_duration=_number("max_duration"),
        sort=sort,
    )


def parse_mark_payload() -> str | None:
    """The mark from a PUT body, or None to clear it.

    Raises:
        HTTPException: 400 if the body is not an object carrying a valid mark.
    """
    body = json_object_body(_MARK_BODY_SHAPE)
    if "mark" not in body:
        abort(400, description=f"Body must carry {_MARK_BODY_SHAPE}.")
    mark = body["mark"]
    if mark is None:
        return None
    if mark not in MARKS:
        abort(400, description=f"Body must carry {_MARK_BODY_SHAPE}.")
    return str(mark)


def parse_prune_payload() -> str | None:
    """Which kind of mark a prune should carry out, or None for every kind.

    An absent `mark` is the deliberate default: the button that prunes
    everything sends no body at all.

    Raises:
        HTTPException: 400 if the body names a mark this library does not have.
    """
    body = request.get_json(silent=True) or {}
    mark = body.get("mark") if isinstance(body, dict) else None
    if mark is None:
        return None
    if mark not in MARKS:
        abort(400, description=f"Unknown mark {mark!r}; expected one of {list(MARKS)}.")
    return str(mark)


def _flag(name: str) -> bool | None:
    """A tri-state query parameter: absent means the filter is not applied."""
    raw = request.args.get(name)
    if not raw:
        return None
    return raw.lower() in ("1", "true", "yes")


def _int(name: str) -> int | None:
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        abort(400, description=f"{name} must be a whole number, not {raw!r}.")


def _number(name: str) -> float | None:
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        abort(400, description=f"{name} must be a number, not {raw!r}.")
