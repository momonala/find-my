"""
Tests for batched Find My report fetching.

src/batch_reports.py drives semi-internal `findmy` APIs, so these tests pin the
behaviour that a library upgrade could silently break:
1. Key groups never exceed Apple's per-list limit
2. Every queried key can be traced back to its accessory
3. A capped round resumes exactly where it stopped, with no gap or overlap
4. Reports are attributed to the right accessory, newest winning
5. Reports for unknown keys are ignored rather than misattributed
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from findmy import KeyPairType

from src.batch_reports import _MAX_KEYS_PER_GROUP
from src.batch_reports import _apply
from src.batch_reports import _build_groups

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


class FakeKey:
    """Stands in for findmy's KeyPair, which needs real crypto to construct."""

    def __init__(self, index: int, slot: int, key_type: KeyPairType) -> None:
        self.hashed_adv_key_b64 = f"{index}-{slot}"
        self.hashed_adv_key_bytes = self.hashed_adv_key_b64.encode()
        self.key_type = key_type


class FakeAccessory:
    """Accessory yielding one primary and one secondary key per rolling index."""

    def __init__(self, name: str = "tag") -> None:
        self.name = name
        self.alignments: list[tuple[datetime, int]] = []

    def keys_at(self, index: int) -> set[FakeKey]:
        return {
            FakeKey(index, 0, KeyPairType.PRIMARY),
            FakeKey(index, 1, KeyPairType.SECONDARY),
        }

    def update_alignment(self, moment: datetime, index: int) -> None:
        self.alignments.append((moment, index))


class FakeReport:
    """Stands in for a LocationReport; decrypt() is a no-op we can assert on."""

    def __init__(self, key_id: str, timestamp: datetime) -> None:
        self.hashed_adv_key_bytes = key_id.encode()
        self.timestamp = timestamp
        self.decrypted_with: FakeKey | None = None

    def decrypt(self, key: FakeKey) -> None:
        self.decrypted_with = key


def test_groups_respect_apple_key_limit():
    accessory = FakeAccessory()
    groups, _ = _build_groups(accessory, start_index=1000, stop_index=0, owners={})

    assert len(groups) > 1, "expected the range to need splitting"
    for primary, secondary in groups:
        assert len(primary) <= _MAX_KEYS_PER_GROUP
        assert len(secondary) <= _MAX_KEYS_PER_GROUP


def test_every_queried_key_is_attributable():
    accessory = FakeAccessory()
    owners: dict = {}
    groups, _ = _build_groups(accessory, start_index=500, stop_index=0, owners=owners)

    queried = {key_id for primary, secondary in groups for key_id in primary + secondary}
    assert queried, "expected keys to be queried"
    for key_id in queried:
        owner, key, _index = owners[key_id.encode()]
        assert owner is accessory
        assert key.hashed_adv_key_b64 == key_id


def test_capped_round_resumes_without_gap_or_overlap():
    accessory = FakeAccessory()
    owners: dict = {}

    probe, resume = _build_groups(accessory, 1000, 0, owners, max_groups=1)
    sweep, _ = _build_groups(accessory, resume, 0, owners)

    assert len(probe) == 1
    probe_ids = {i for p, s in probe for i in p + s}
    sweep_ids = {i for p, s in sweep for i in p + s}
    assert not probe_ids & sweep_ids, "rounds must not re-query the same key"

    expected = {f"{index}-{slot}" for index in range(0, 1001) for slot in (0, 1)}
    assert probe_ids | sweep_ids == expected, "rounds must cover the range exactly"


def test_uncapped_build_covers_whole_range():
    accessory = FakeAccessory()
    _groups, resume = _build_groups(accessory, 100, 50, owners={})

    assert resume < 50


def test_apply_keeps_newest_report_per_accessory():
    accessory = FakeAccessory()
    owners: dict = {}
    _build_groups(accessory, 10, 0, owners)

    older = FakeReport("5-0", _NOW - timedelta(hours=2))
    newer = FakeReport("7-0", _NOW)
    newest: dict = {}

    _apply([older, newer], owners, newest)

    assert newest[accessory] is newer
    assert newer.decrypted_with is not None, "reports must be decrypted with their own key"
    assert (_NOW, 7) in accessory.alignments


def test_apply_ignores_unknown_keys():
    accessory = FakeAccessory()
    owners: dict = {}
    _build_groups(accessory, 10, 0, owners)
    newest: dict = {}

    _apply([FakeReport("999-0", _NOW)], owners, newest)

    assert newest == {}
    assert accessory.alignments == []
