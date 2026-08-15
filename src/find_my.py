"""List Apple devices signed into iCloud via the classic Find My iPhone API.

Covers iPhones, iPads, Macs and AirPods — hardware that reports its own
location. AirTags and third-party trackers are not served by this API at all;
see src/airtags.py for those.

Authenticates via pyicloud, prompting for a 2FA code on first run. Session
cookies are cached in .icloud_session/ so later runs skip verification.
"""

import sys
from datetime import UTC
from datetime import datetime

import typer
from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudAuthRequiredException
from pyicloud.exceptions import PyiCloudFailedLoginException

from src.errors import InteractiveAuthRequiredError
from src.errors import LoginFailedError
from src.errors import TwoFactorRejectedError
from src.tracking import SESSION_DIR
from src.tracking import Location
from src.tracking import TrackedItem
from src.tracking import require_credentials

# Mirrors POLL_INTERVAL_SECONDS in src/poller.py -- not imported from there
# because the poller imports this module. pyicloud refreshes device locations on
# a background thread at this interval, so leaving it at pyicloud's five-minute
# default would have the poller rewriting the same stale fix for five cycles.
_DEVICE_REFRESH_SECONDS = 60

_api: PyiCloudService | None = None


def _authenticate() -> PyiCloudService:
    """Return an authenticated pyicloud session, reusing cached cookies if valid.

    Raises:
        MissingCredentialsError, LoginFailedError, TwoFactorRejectedError.
        InteractiveAuthRequiredError: If Apple wants a 2FA code and there's no
            terminal to prompt on (the background poller's case).
    """
    username, password = require_credentials()
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    try:
        api = PyiCloudService(
            username,
            password,
            cookie_directory=str(SESSION_DIR),
            refresh_interval=_DEVICE_REFRESH_SECONDS,
        )
    except PyiCloudFailedLoginException as error:
        raise LoginFailedError(f"Login failed: {error}") from error

    if api.requires_2fa:
        if not sys.stdin.isatty():
            raise InteractiveAuthRequiredError(
                "Apple requires a 2FA code, but there is no terminal to prompt on. "
                "Run `uv run findmy devices` once at the console, then retry."
            )
        code = typer.prompt("Enter the 2FA code sent to your trusted device")
        if not api.validate_2fa_code(code):
            raise TwoFactorRejectedError("2FA code was not accepted.")
        if not api.is_trusted_session:
            api.trust_session()

    return api


def _get_api() -> PyiCloudService:
    """Return the process-wide pyicloud session, authenticating on first use.

    Rebuilding this every poll cycle is what leaked: `_authenticate()` opens a
    fresh `requests.Session`, and the first touch of `api.devices` starts a
    background refresh thread that holds a reference back to the whole session
    graph, so each discarded session stayed reachable forever.
    """
    global _api
    if _api is None:
        _api = _authenticate()
    return _api


def _discard_api() -> None:
    """Drop the cached session, stopping the refresh thread that pins it alive.

    `FindMyiPhoneServiceManager` runs a daemon thread holding a reference back
    to the manager, so dropping the last reference is not enough to collect it
    -- the monitor has to be told to stop. pyicloud's own session reset skips
    this, which is why it is done by hand here.
    """
    global _api
    manager = getattr(_api, "_devices", None)
    if manager is not None:
        manager.stop_event.set()
    _api = None


def _to_location(raw: dict) -> Location:
    """Convert a pyicloud location payload, whose timestamp is epoch milliseconds."""
    return Location(
        latitude=raw["latitude"],
        longitude=raw["longitude"],
        seen_at=datetime.fromtimestamp(raw["timeStamp"] / 1000, tz=UTC),
    )


def _collect_devices(api: PyiCloudService) -> list[TrackedItem]:
    return [
        TrackedItem(
            id=str(device.data["id"]),
            name=device.name,
            kind=device.device_type,
            source="device",
            location=_to_location(device.location) if device.location else None,
        )
        for device in api.devices
    ]


def fetch_devices() -> list[TrackedItem]:
    """Return every Apple device on the account with its last known location.

    A cached session Apple has since expired surfaces as
    `PyiCloudAuthRequiredException`. That's worth one silent re-login here:
    the session is now long-lived, so without it a single expiry would wedge
    the poller until the process was restarted.
    """
    try:
        return _collect_devices(_get_api())
    except PyiCloudAuthRequiredException:
        _discard_api()
        return _collect_devices(_get_api())
