"""Locate many Find My accessories in a handful of batched requests.

`findmy`'s own `fetch_location()` queries one accessory at a time, and each
accessory walks its rolling keys backwards until it finds a report — so a
tracker with nothing recent scans a full week on its own. That costs one HTTP
request (and one ~40ms Anisette header generation) per 290 keys, which for a
dozen trackers is tens of sequential round trips.

Apple's reports endpoint accepts a *list* of key groups per request, and the
290-key limit applies per group rather than per request: 10 accessories and
2,910 keys came back in one 0.34s call. This module exploits that with two
rounds — a cheap probe of everyone's newest keys, then one exhaustive sweep for
whoever didn't answer.

Concurrency is deliberately absent. Every request needs Anisette headers from an
emulated ARM library that collapses under parallel use (~10s per call with five
concurrent lookups, versus ~40ms alone), so requests are issued one at a time
and the win comes from making far fewer of them.

This reaches past `findmy`'s public API into `fetch_raw_reports`,
`LocationReport.decrypt` and `FindMyAccessory.update_alignment`, so the
`findmy` version is pinned and tests/test_batch_reports.py checks the results
still match `fetch_location()`.
"""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from findmy import AsyncAppleAccount
from findmy import KeyPairType
from findmy import LocationReport
from findmy.accessory import FindMyAccessory
from findmy.keys import KeyPair

logger = logging.getLogger(__name__)

# Apple rejects more than this many ids in a single group.
_MAX_KEYS_PER_GROUP = 290
# Groups per HTTP request. 10 is verified against Apple; this stays close to it
# because a rejected request costs a whole round trip to discover.
_MAX_GROUPS_PER_REQUEST = 12
# fetch_raw_reports hardcodes a 7-day window server-side, so looking further
# back is pointless.
_SEARCH_WINDOW = timedelta(days=7)

# One accessory's keys for one request group, as (primary ids, secondary ids).
_KeyGroup = tuple[list[str], list[str]]
# Which accessory, key and rolling index a returned report belongs to.
_KeyOwners = dict[bytes, tuple[FindMyAccessory, KeyPair, int]]


def _build_groups(
    accessory: FindMyAccessory,
    start_index: int,
    stop_index: int,
    owners: _KeyOwners,
    max_groups: int | None = None,
) -> tuple[list[_KeyGroup], int]:
    """Chunk `accessory`'s keys from `start_index` down to `stop_index` into groups.

    Returns the groups and the index to resume from, so a later round can pick up
    where a capped one left off.
    """
    groups: list[_KeyGroup] = []
    primary: list[str] = []
    secondary: list[str] = []
    index = start_index

    while index >= stop_index:
        if max_groups is not None and len(groups) >= max_groups:
            return groups, index

        keys = accessory.keys_at(index)
        # Mirrors findmy: anything that is not PRIMARY is queried as secondary.
        new_primary = [k for k in keys if k.key_type == KeyPairType.PRIMARY]
        new_secondary = [k for k in keys if k.key_type != KeyPairType.PRIMARY]

        would_overflow = (
            len(primary) + len(new_primary) > _MAX_KEYS_PER_GROUP
            or len(secondary) + len(new_secondary) > _MAX_KEYS_PER_GROUP
        )
        if would_overflow:
            groups.append((primary, secondary))
            primary, secondary = [], []
            continue  # re-derive this index into the fresh group

        for key in keys:
            owners[key.hashed_adv_key_bytes] = (accessory, key, index)
        primary.extend(k.hashed_adv_key_b64 for k in new_primary)
        secondary.extend(k.hashed_adv_key_b64 for k in new_secondary)
        index -= 1

    if primary or secondary:
        groups.append((primary, secondary))
    return groups, index


async def _fetch(account: AsyncAppleAccount, groups: list[_KeyGroup]) -> list[LocationReport]:
    """Fetch every group, splitting into as few requests as Apple allows."""
    reports: list[LocationReport] = []
    for start in range(0, len(groups), _MAX_GROUPS_PER_REQUEST):
        chunk = groups[start : start + _MAX_GROUPS_PER_REQUEST]
        keys = sum(len(p) + len(s) for p, s in chunk)
        logger.debug("Requesting %d groups (%d keys)", len(chunk), keys)
        reports.extend(await account.fetch_raw_reports(chunk))
    return reports


def _apply(
    reports: list[LocationReport],
    owners: _KeyOwners,
    newest: dict[FindMyAccessory, LocationReport],
) -> None:
    """Decrypt reports, attribute them to accessories, and keep the newest each."""
    for report in reports:
        owner = owners.get(report.hashed_adv_key_bytes)
        if owner is None:
            # A report for a key we did not ask for; nothing sensible to do.
            continue

        accessory, key, index = owner
        report.decrypt(key)
        accessory.update_alignment(report.timestamp, index)

        current = newest.get(accessory)
        if current is None or report.timestamp > current.timestamp:
            newest[accessory] = report


async def locate_accessories(
    accessories: list[FindMyAccessory],
    account: AsyncAppleAccount,
) -> dict[FindMyAccessory, LocationReport | None]:
    """Return the most recent location report for each accessory, or None."""
    now = datetime.now(UTC)
    owners: _KeyOwners = {}
    newest: dict[FindMyAccessory, LocationReport] = {}
    # Per accessory: where the next round should resume, and how far back to look.
    resume: dict[FindMyAccessory, int] = {}
    oldest: dict[FindMyAccessory, int] = {}

    # Round 1: everyone's newest keys, which is all a recently-seen tracker needs.
    probe: list[_KeyGroup] = []
    for accessory in accessories:
        oldest[accessory] = accessory.get_min_index(now - _SEARCH_WINDOW)
        groups, next_index = _build_groups(
            accessory,
            accessory.get_max_index(now),
            oldest[accessory],
            owners,
            max_groups=1,
        )
        resume[accessory] = next_index
        probe.extend(groups)

    _apply(await _fetch(account, probe), owners, newest)

    # Round 2: sweep the rest of the window for whoever stayed silent. Batching
    # every remaining group into one sweep is what keeps a dead tracker from
    # costing a dozen serial round trips.
    sweep: list[_KeyGroup] = []
    for accessory in accessories:
        if accessory in newest or resume[accessory] < oldest[accessory]:
            continue
        groups, _ = _build_groups(accessory, resume[accessory], oldest[accessory], owners)
        sweep.extend(groups)

    if sweep:
        logger.debug(
            "Sweeping %d groups for %d silent accessories", len(sweep), len(accessories) - len(newest)
        )
        _apply(await _fetch(account, sweep), owners, newest)

    return {accessory: newest.get(accessory) for accessory in accessories}
