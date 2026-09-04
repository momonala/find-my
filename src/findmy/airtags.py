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
tracker; nothing else writes the key cache. Rolling-key alignment lives in the
`tracker_alignment` table, not in that file.
"""

import asyncio
import gc
import hashlib
import json
import logging
import os
import platform
import re
import sqlite3
import sys
from datetime import UTC
from datetime import datetime
from typing import Any

import typer
from findmy.accessory import FindMyAccessory
from findmy.plist import list_accessories

from findmy import AppleAccount
from findmy import AsyncAppleAccount
from findmy import LocalAnisetteProvider
from findmy import LocationReport
from findmy import LoginState
from findmy import TrustedDeviceSecondFactorMethod
from src.core.db import connection
from src.core.errors import InteractiveAuthRequiredError
from src.core.errors import TwoFactorRejectedError
from src.core.errors import UnsupportedPlatformError
from src.core.paths import SESSION_DIR
from src.findmy.batch_reports import locate_accessories
from src.findmy.db import load_tracker_alignment
from src.findmy.db import save_tracker_alignment
from src.findmy.tracking import Location
from src.findmy.tracking import TrackedItem
from src.findmy.tracking import require_credentials

logger = logging.getLogger(__name__)

_ACCOUNT_FILE = SESSION_DIR / "findmy_account.json"
_ANISETTE_LIBS = SESSION_DIR / "ani_libs.bin"
# Holds each tracker's master key in plaintext, so it stays owner-read-only and
# inside the git-ignored session directory. Anyone with this file can locate
# these trackers; delete it to fall back to reading the Keychain. Read-only on
# every path except --refresh-keys.
_TRACKERS_FILE = SESSION_DIR / "trackers.json"

# Apple's own devices report a model like "iPhone14,5" or "MacBookPro11,4",
# while AirTags and third-party trackers use human-readable names such as
# "AirTag (2nd generation)" or "Sualio Tag". Apple devices appear in the local
# key store too, but src/findmy/devices.py already covers them — and skipping them here
# avoids a slow historical rolling-key scan for hardware that rarely reports.
_APPLE_DEVICE_MODEL = re.compile(r"^[A-Za-z]+\d+,\d+$")

_UNSUPPORTED_PLATFORM_MSG = (
    "This operation requires macOS 14+. "
    "Run it on the Mac first, then copy .icloud_session/ and trackers.json here."
)

# Bits 6-7 of a LocationReport's status byte, per the Offline Finding BLE
# advertisement format -- the same two bits findmy.scanner.scanner decodes for
# a directly-scanned beacon. Apple's crowdsourced network already hands us
# this byte in every report, so no local Bluetooth radio is needed to read it.
_BATTERY_LEVELS = {0b00: "Full", 0b01: "Medium", 0b10: "Low", 0b11: "Very Low"}


def _battery_level(report: LocationReport) -> str | None:
    try:
        status = report.status
    except RuntimeError:
        return None
    return _BATTERY_LEVELS.get((status >> 6) & 0b11)


def is_macos_14_plus() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        major = int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return False
    return major >= 14


def _is_tracker(accessory: FindMyAccessory) -> bool:
    return not _APPLE_DEVICE_MODEL.match(accessory.model or "")


def _stable_id(accessory: FindMyAccessory) -> str:
    """A stable per-accessory ID, for third-party tags that lack `identifier`.

    `master_key` is fixed at pairing, so hashing it is as stable as `identifier`
    itself without depending on Apple having populated that field.
    """
    return accessory.identifier or hashlib.sha256(accessory.master_key).hexdigest()[:16]


def _save_trackers(trackers: list[FindMyAccessory]) -> None:
    """Write the tracker key cache. Reached only by `--refresh-keys`.

    Staged and renamed, so an interrupted write cannot truncate master keys a
    non-macOS host cannot regenerate. The pid in the temp name keeps concurrent
    refreshes off each other's staging file; they then race on an atomic rename.

    The mode is set at creation rather than chmod'ed after, which would leave
    the keys world-readable for the length of the write. 0o400 still permits a
    later refresh: the rename needs write permission on the directory, not the
    file.
    """
    payload = json.dumps([tracker.to_json() for tracker in trackers], indent=2)
    staged = _TRACKERS_FILE.with_suffix(f".{os.getpid()}.tmp")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with open(descriptor, "w") as handle:  # noqa: PTH123 -- Path.open() can't take a raw fd
            handle.write(payload)
        staged.replace(_TRACKERS_FILE)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _apply_alignment(trackers: list[FindMyAccessory]) -> None:
    """Fast-forward each tracker's rolling-key alignment from the database.

    Alignment is how far through its key rotation a tracker was last seen;
    without it a locate rescans up to a week of keys. Purely a cache, so every
    miss degrades rather than fails: `update_alignment` keeps the newer value,
    and a database error is logged and skipped -- `mycloud airtags` and
    `mycloud all` locate without ever calling `init_db()`, so the table may not
    exist at all.
    """
    try:
        with connection() as conn:
            stored = load_tracker_alignment(conn)
    except sqlite3.Error as error:
        logger.warning("Could not read tracker alignment (%s); falling back to the key cache", error)
        return

    for tracker in trackers:
        entry = stored.get(_stable_id(tracker))
        if entry is None:
            continue
        alignment_date, alignment_index = entry
        tracker.update_alignment(datetime.fromisoformat(alignment_date), alignment_index)


def _persist_alignment(trackers: list[FindMyAccessory]) -> None:
    """Record where this locate left each tracker's key rotation.

    Reads the two fields directly because `findmy` exposes no getter, and its
    public `to_json()` would hex-encode the master key, skn and sks into fresh
    strings once a minute just to read an integer. A database error is logged
    and skipped, as in `_apply_alignment`.
    """
    alignment = {
        _stable_id(tracker): (
            tracker._alignment_date.isoformat(),  # noqa: SLF001 -- see above
            tracker._alignment_index,  # noqa: SLF001 -- see above
        )
        for tracker in trackers
    }
    try:
        with connection() as conn:
            save_tracker_alignment(conn, alignment)
    except sqlite3.Error as error:
        logger.warning("Could not store tracker alignment (%s); the next locate will sweep", error)


def load_trackers(refresh_keys: bool = False) -> list[FindMyAccessory]:
    """Return this Mac's trackers, preferring the cache over the Keychain.

    A tracker's keys are fixed when it is paired, so re-reading them every run
    only costs a Keychain prompt. Pass `refresh_keys` after pairing a new one.
    Reading from the cache is supported on any platform; refreshing from the
    Keychain requires macOS 14+.
    """
    if _TRACKERS_FILE.exists() and not refresh_keys:
        trackers = [FindMyAccessory.from_json(entry) for entry in json.loads(_TRACKERS_FILE.read_text())]
        _apply_alignment(trackers)
        return trackers

    if not is_macos_14_plus():
        raise UnsupportedPlatformError(_UNSUPPORTED_PLATFORM_MSG)

    trackers = [accessory for accessory in list_accessories() if _is_tracker(accessory)]
    _save_trackers(trackers)
    return trackers


def _ensure_session() -> None:
    """Log in if needed, leaving a session in `_ACCOUNT_FILE` for later runs.

    A cached session is checked before credentials are, so a host with a warm
    `.icloud_session/` keeps working without a `.env` -- which is exactly the
    deployed case, since `.env` is git-ignored and never copied to the remote.
    Initial login (no cached session) requires macOS 14+; on Linux the session
    must be seeded from a Mac.

    Raises:
        MissingCredentialsError, InteractiveAuthRequiredError,
        TwoFactorRejectedError, UnsupportedPlatformError.
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    if _ACCOUNT_FILE.exists():
        return

    if not is_macos_14_plus():
        raise UnsupportedPlatformError(_UNSUPPORTED_PLATFORM_MSG)

    username, password = require_credentials()
    account = AppleAccount(LocalAnisetteProvider(libs_path=_ANISETTE_LIBS))
    if account.login(username, password) == LoginState.REQUIRE_2FA:
        if not sys.stdin.isatty():
            raise InteractiveAuthRequiredError(
                "Apple requires a 2FA code, but there is no terminal to prompt on. "
                "Run `uv run mycloud airtags` once at the console, then retry."
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


_event_loop: asyncio.AbstractEventLoop | None = None


def _get_event_loop() -> asyncio.AbstractEventLoop:
    """Return a process-wide event loop, reused across poll cycles.

    `asyncio.run()` creates a fresh loop (and its default `ThreadPoolExecutor`)
    on every call and tears it down afterwards; at a 60-second poll interval
    that leaks accumulating idle executor threads. Reusing one loop lets the
    executor's threads be reused instead of recreated each cycle.
    """
    global _event_loop
    if _event_loop is None or _event_loop.is_closed():
        _event_loop = asyncio.new_event_loop()
    return _event_loop


_anisette_provider: LocalAnisetteProvider | None = None
_anisette_uses = 0
# Header generations per provider before the VM is rebuilt. Each generation
# leaks ~40-50 kB of emulator JIT buffer; 200 caps that at ~10 MB for the price
# of one rebuild every few hours. See "Why RAM saw-tooths" in README.md.
_ANISETTE_MAX_USES = 200


def _get_anisette_provider(state: dict[str, Any]) -> LocalAnisetteProvider:
    """Return the process-wide Anisette provider, rebuilt every `_ANISETTE_MAX_USES` uses.

    Building one spins up a unicorn VM, so it is reused across poll cycles. It
    cannot be reused indefinitely: the emulator's JIT buffer only shrinks when
    the VM is freed. Rebuilding from `state` picks up the provisioning data as
    it is on disk now.

    Both teardown lines are required, and dropping either makes memory worse
    than no recycling -- `Closable.__del__` resurrects a collected provider onto
    the shared event loop, and the VM is only reachable through a reference
    cycle the counts-based collector will not trigger on. See "Why RAM
    saw-tooths" in README.md.
    """
    global _anisette_provider, _anisette_uses
    if _anisette_provider is not None and _anisette_uses >= _ANISETTE_MAX_USES:
        _anisette_provider._ani = None  # noqa: SLF001 -- see above; no public equivalent
        _anisette_provider = None
        _anisette_uses = 0
        gc.collect()
    if _anisette_provider is None:
        _anisette_provider = LocalAnisetteProvider.from_json(state["anisette"], libs_path=_ANISETTE_LIBS)
    _anisette_uses += 1
    return _anisette_provider


async def _locate(
    accessories: list[FindMyAccessory],
) -> dict[FindMyAccessory, LocationReport | None]:
    """Locate accessories over a restored session, saving refreshed session state.

    The saved Anisette provisioning state has to be restored alongside the
    session — building a fresh provider makes Apple reject the requests and
    demand 2FA again.
    """
    state = json.loads(_ACCOUNT_FILE.read_text())
    account = AsyncAppleAccount(_get_anisette_provider(state), state_info=state)
    try:
        locations = await locate_accessories(accessories, account)
        account.to_json(_ACCOUNT_FILE)
        return locations
    finally:
        await account.close()


def fetch_airtags(refresh_keys: bool = False) -> list[TrackedItem]:
    """Return the trackers paired to this Mac with their last known location.

    On non-macOS-14+ hosts with no cached session or tracker keys this returns
    an empty list rather than failing -- both must be seeded from a Mac first.
    """
    if not is_macos_14_plus() and (not _ACCOUNT_FILE.exists() or not _TRACKERS_FILE.exists()):
        return []
    _ensure_session()
    accessories = load_trackers(refresh_keys)
    if not accessories:
        return []

    locations = _get_event_loop().run_until_complete(_locate(accessories))
    # Locating advances alignment in place. It is persisted to the database,
    # never back into the key cache.
    _persist_alignment(accessories)

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
            battery_level=_battery_level(report) if report else None,
        )
        for accessory, report in locations.items()
    ]


def has_cached_keys() -> bool:
    """Whether tracker keys are cached, i.e. whether a run needs the Keychain."""
    return _TRACKERS_FILE.exists()
