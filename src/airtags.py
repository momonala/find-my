"""List AirTag locations via Apple's crowdsourced Find My network.

AirTags have no network connection of their own: nearby Apple devices relay
encrypted Bluetooth beacon reports to Apple, which this module decrypts using
the trackers' private keys read from this Mac's local Find My data
(~/Library/com.apple.icloud.searchpartyd). That means it only works for
trackers paired via the Find My app on this Mac, and reading the keys triggers a
macOS Keychain prompt — so it must run at the console, not over SSH.

Session state is cached in .icloud_session/ so 2FA isn't required on every run,
and so are the tracker keys — they are fixed at pairing, and caching them keeps
the Keychain prompt to first run only. Pass --refresh-keys after pairing a new
tracker.
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import UTC

import typer
from findmy import AppleAccount
from findmy import AsyncAppleAccount
from findmy import LocalAnisetteProvider
from findmy import LocationReport
from findmy import LoginState
from findmy import TrustedDeviceSecondFactorMethod
from findmy.accessory import FindMyAccessory
from findmy.plist import list_accessories

from src.batch_reports import locate_accessories
from src.errors import InteractiveAuthRequiredError
from src.errors import TwoFactorRejectedError
from src.tracking import SESSION_DIR
from src.tracking import Location
from src.tracking import TrackedItem
from src.tracking import require_credentials

_ACCOUNT_FILE = SESSION_DIR / "findmy_account.json"
_ANISETTE_LIBS = SESSION_DIR / "ani_libs.bin"
# Holds each tracker's master key in plaintext, so it stays chmod 600 and inside
# the git-ignored session directory. Anyone with this file can locate these
# trackers; delete it to fall back to reading the Keychain.
_TRACKERS_FILE = SESSION_DIR / "trackers.json"

# Apple's own devices report a model like "iPhone14,5" or "MacBookPro11,4",
# while AirTags and third-party trackers use human-readable names such as
# "AirTag (2nd generation)" or "Sualio Tag". Apple devices appear in the local
# key store too, but src/find_my.py already covers them — and skipping them here
# avoids a slow historical rolling-key scan for hardware that rarely reports.
_APPLE_DEVICE_MODEL = re.compile(r"^[A-Za-z]+\d+,\d+$")


def _is_tracker(accessory: FindMyAccessory) -> bool:
    return not _APPLE_DEVICE_MODEL.match(accessory.model or "")


def _stable_id(accessory: FindMyAccessory) -> str:
    """A stable per-accessory ID, for third-party tags that lack `identifier`.

    `master_key` is fixed at pairing, so hashing it is as stable as `identifier`
    itself without depending on Apple having populated that field.
    """
    return accessory.identifier or hashlib.sha256(accessory.master_key).hexdigest()[:16]


def _save_trackers(trackers: list[FindMyAccessory]) -> None:
    """Cache tracker keys and their rolling-key alignment, owner-readable only.

    The mode is applied *before* the keys are written -- creating the file with
    the default umask and chmod'ing afterwards would leave the master keys
    world-readable for the duration of the write.
    """
    payload = json.dumps([t.to_json() for t in trackers], indent=2)
    descriptor = os.open(_TRACKERS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(descriptor, "w") as handle:  # noqa: PTH123 -- Path.open() can't take a raw fd
        handle.write(payload)
    # os.open() won't lower the mode of a file that already existed.
    _TRACKERS_FILE.chmod(0o600)


def load_trackers(refresh_keys: bool = False) -> list[FindMyAccessory]:
    """Return this Mac's trackers, preferring the cache over the Keychain.

    A tracker's keys are fixed when it is paired, so re-reading them every run
    only costs a Keychain prompt. Pass `refresh_keys` after pairing a new one.
    """
    if _TRACKERS_FILE.exists() and not refresh_keys:
        return [FindMyAccessory.from_json(entry) for entry in json.loads(_TRACKERS_FILE.read_text())]

    trackers = [a for a in list_accessories() if _is_tracker(a)]
    _save_trackers(trackers)
    return trackers


def _ensure_session() -> None:
    """Log in if needed, leaving a session in `_ACCOUNT_FILE` for later runs.

    A cached session is checked before credentials are, so a host with a warm
    `.icloud_session/` keeps working without a `.env` -- which is exactly the
    deployed case, since `.env` is git-ignored and never copied to the remote.

    Raises:
        MissingCredentialsError, InteractiveAuthRequiredError,
        TwoFactorRejectedError.
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    if _ACCOUNT_FILE.exists():
        return

    username, password = require_credentials()
    account = AppleAccount(LocalAnisetteProvider(libs_path=_ANISETTE_LIBS))
    if account.login(username, password) == LoginState.REQUIRE_2FA:
        if not sys.stdin.isatty():
            raise InteractiveAuthRequiredError(
                "Apple requires a 2FA code, but there is no terminal to prompt on. "
                "Run `uv run findmy airtags` once at the console, then retry."
            )
        method = next(
            (m for m in account.get_2fa_methods() if isinstance(m, TrustedDeviceSecondFactorMethod)),
            None,
        )
        if method is None:
            raise TwoFactorRejectedError(
                "Apple requires 2FA but offered no trusted-device method to satisfy it."
            )
        method.request()
        method.submit(typer.prompt("Enter the 2FA code sent to your trusted device"))

    account.to_json(_ACCOUNT_FILE)


async def _locate(
    accessories: list[FindMyAccessory],
) -> dict[FindMyAccessory, LocationReport | None]:
    """Locate accessories over a restored session, saving refreshed session state.

    The saved Anisette provisioning state has to be restored alongside the
    session — building a fresh provider makes Apple reject the requests and
    demand 2FA again.
    """
    state = json.loads(_ACCOUNT_FILE.read_text())
    account = AsyncAppleAccount(
        LocalAnisetteProvider.from_json(state["anisette"], libs_path=_ANISETTE_LIBS),
        state_info=state,
    )
    try:
        locations = await locate_accessories(accessories, account)
        account.to_json(_ACCOUNT_FILE)
        return locations
    finally:
        await account.close()


def fetch_airtags(refresh_keys: bool = False) -> list[TrackedItem]:
    """Return the trackers paired to this Mac with their last known location."""
    _ensure_session()
    accessories = load_trackers(refresh_keys)
    if not accessories:
        return []

    locations = asyncio.run(_locate(accessories))
    # Locating advances each accessory's rolling-key alignment in place;
    # persisting it here is what keeps later runs from rescanning weeks of keys.
    _save_trackers(accessories)

    return [
        TrackedItem(
            id=_stable_id(accessory),
            name=accessory.name or f"Unnamed {accessory.model}",
            kind=accessory.model or "unknown",
            source="item",
            location=(
                Location(
                    latitude=report.latitude,
                    longitude=report.longitude,
                    seen_at=report.timestamp.astimezone(UTC),
                )
                if report
                else None
            ),
        )
        for accessory, report in locations.items()
    ]


def has_cached_keys() -> bool:
    """Whether tracker keys are cached, i.e. whether a run needs the Keychain."""
    return _TRACKERS_FILE.exists()
