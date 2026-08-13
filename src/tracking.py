"""Shared domain model and rendering for Find My items.

Apple exposes two unrelated location systems, so this project has two sources:
src/find_my.py for devices signed into iCloud (classic Find My iPhone API) and
src/airtags.py for AirTags and third-party trackers (crowdsourced Find My
network). Both return `list[TrackedItem]` and render through `render_items`, so
callers treat them the same way.
"""

import enum
import json
import math
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.config import HOME_LATITUDE
from src.config import HOME_LONGITUDE
from src.env import ICLOUD_PASSWORD
from src.env import ICLOUD_USERNAME
from src.errors import MissingCredentialsError

SESSION_DIR = Path(__file__).resolve().parent.parent / ".icloud_session"

_console = Console()
_EARTH_RADIUS_M = 6_371_008.8
_STALE_AFTER_MINUTES = 60


@dataclass(frozen=True)
class Location:
    """A position fix: where something was, and when it was observed there."""

    latitude: float
    longitude: float
    seen_at: datetime  # timezone-aware


@dataclass(frozen=True)
class TrackedItem:
    """A Find My device or tracker, with its last known location if it has one.

    `kind` is whatever the source calls the hardware: a device class such as
    "iPhone" for Apple devices, or a model name such as "AirTag (2nd
    generation)" for trackers. `source` is which backend produced it --
    "device" for src/find_my.py, "item" for src/airtags.py -- so the dashboard
    can split them into tabs the way the Find My app does.
    """

    id: str
    name: str
    kind: str
    source: str
    location: Location | None


class SortKey(str, enum.Enum):
    """How to order items for output. Items with no location always sort last."""

    NAME = "name"
    DISTANCE = "distance"
    AGE = "age"


def require_credentials() -> tuple[str, str]:
    """Return the iCloud username and password.

    Raises:
        MissingCredentialsError: If either is unset. Callers decide how to
            report it -- see src/errors.py for why this isn't a `typer.Exit`.
    """
    if not ICLOUD_USERNAME or not ICLOUD_PASSWORD:
        raise MissingCredentialsError("Set ICLOUD_USERNAME and ICLOUD_PASSWORD in .env first.")
    return ICLOUD_USERNAME, ICLOUD_PASSWORD


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in meters.

    This is the project's only haversine -- the dashboard reads `distance_m`
    off the API rather than recomputing it, and src/alerts.py's movement
    check reuses this rather than a second implementation.
    """
    rad_lat1, rad_lon1 = math.radians(lat1), math.radians(lon1)
    rad_lat2, rad_lon2 = math.radians(lat2), math.radians(lon2)

    half_chord = (
        math.sin((rad_lat2 - rad_lat1) / 2) ** 2
        + math.cos(rad_lat1) * math.cos(rad_lat2) * math.sin((rad_lon2 - rad_lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(half_chord))


def distance_from_home_m_at(latitude: float, longitude: float) -> float:
    """Great-circle distance from the configured home coordinates, in meters.

    Takes raw coordinates so callers holding a DB row (src/api.py) can use it
    without first building a `Location`.
    """
    return haversine_m(HOME_LATITUDE, HOME_LONGITUDE, latitude, longitude)


def distance_from_home_m(location: Location) -> float:
    """Great-circle distance from home to a `Location`, in meters."""
    return distance_from_home_m_at(location.latitude, location.longitude)


def minutes_ago(moment: datetime, now: datetime) -> float:
    """Minutes elapsed between `moment` and `now`."""
    return (now - moment).total_seconds() / 60


def sort_items(items: list[TrackedItem], sort_key: SortKey) -> list[TrackedItem]:
    """Order items, keeping those without a location at the end of the list."""
    now = datetime.now(UTC)

    def rank(item: TrackedItem) -> tuple[bool, float | str]:
        if item.location is None:
            return (True, "")
        if sort_key is SortKey.NAME:
            return (False, item.name.lower())
        if sort_key is SortKey.DISTANCE:
            return (False, distance_from_home_m(item.location))
        return (False, minutes_ago(item.location.seen_at, now))

    return sorted(items, key=rank)


def items_to_json(items: list[TrackedItem]) -> str:
    """Serialize items with their derived distance and age, for piping elsewhere."""
    now = datetime.now(UTC)
    payload = [
        {
            "id": item.id,
            "name": item.name,
            "kind": item.kind,
            "source": item.source,
            "latitude": item.location.latitude if item.location else None,
            "longitude": item.location.longitude if item.location else None,
            "seen_at": item.location.seen_at.isoformat() if item.location else None,
            "distance_m": round(distance_from_home_m(item.location)) if item.location else None,
            "age_minutes": (round(minutes_ago(item.location.seen_at, now)) if item.location else None),
        }
        for item in items
    ]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def render_items(items: list[TrackedItem], title: str, elapsed_seconds: float) -> None:
    """Print items as a table, colored by how recently each one was seen."""
    table = Table(title=title, caption=f"{len(items)} items in {elapsed_seconds:.1f}s")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Location")
    table.add_column("Distance", justify="right")
    table.add_column("Age", justify="right")
    table.add_column("Last seen")

    now = datetime.now(UTC)
    for item in items:
        if item.location is None:
            table.add_row(item.name, item.kind, "unavailable", "-", "-", "-", style="dim")
            continue

        location = item.location
        age_minutes = minutes_ago(location.seen_at, now)
        table.add_row(
            item.name,
            item.kind,
            f"{location.latitude:.6f}, {location.longitude:.6f}",
            f"{distance_from_home_m(location):,.0f} m",
            f"{age_minutes:,.0f} min ago",
            location.seen_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            style="yellow" if age_minutes > _STALE_AFTER_MINUTES else "green",
        )

    _console.print(table)
