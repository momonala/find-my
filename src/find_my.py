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
from pyicloud.exceptions import PyiCloudFailedLoginException

from src.errors import InteractiveAuthRequiredError
from src.errors import LoginFailedError
from src.errors import TwoFactorRejectedError
from src.tracking import SESSION_DIR
from src.tracking import Location
from src.tracking import TrackedItem
from src.tracking import require_credentials


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
        api = PyiCloudService(username, password, cookie_directory=str(SESSION_DIR))
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


def _to_location(raw: dict) -> Location:
    """Convert a pyicloud location payload, whose timestamp is epoch milliseconds."""
    return Location(
        latitude=raw["latitude"],
        longitude=raw["longitude"],
        seen_at=datetime.fromtimestamp(raw["timeStamp"] / 1000, tz=UTC),
    )


def fetch_devices() -> list[TrackedItem]:
    """Return every Apple device on the account with its last known location."""
    api = _authenticate()
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
