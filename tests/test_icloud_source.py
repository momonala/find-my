"""Finding the live assets a download needs.

The rank semantics are counter-intuitive and were confirmed against the real
API: rank 0 is an album's *newest* asset, and ASCENDING reads forward from a
rank towards older assets. So paging ranks upward with ASCENDING walks newest
to oldest, and a video just taken is in the first page.

The fake album below reproduces exactly that, so the walk stays pinned against
someone (reasonably) assuming DESCENDING is what gives newest-first.
"""

from dataclasses import dataclass
from dataclasses import field

import pytest
from pyicloud.services.photos_cloudkit.constants import DirectionEnum

from icloud_photo_metadata.icloud_source import find_assets


@dataclass
class FakeAsset:
    id: str


@dataclass
class FakeAlbum:
    """An album ranked newest-first, the way CloudKit ranks one."""

    size: int
    page_size: int = 100
    requests: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.assets = [FakeAsset(id=f"asset-{rank:04d}") for rank in range(self.size)]

    def __len__(self) -> int:
        return self.size

    def _get_photos_at(self, start_rank: int, direction: DirectionEnum, page_size: int) -> list[FakeAsset]:
        assert direction is DirectionEnum.ASCENDING
        self.requests.append(start_rank)
        return self.assets[start_rank : start_rank + page_size]


@dataclass
class FakeApi:
    album: FakeAlbum | None

    @property
    def photos(self) -> "FakeApi":
        return self

    @property
    def albums(self) -> dict[str, FakeAlbum | None]:
        return {"Library": self.album}


@pytest.mark.parametrize("size", [0, 1, 99, 100, 101, 250, 9619])
def test_every_wanted_asset_is_found_whatever_the_album_size(size: int) -> None:
    """A page-boundary slip would silently leave a video unbacked."""
    album = FakeAlbum(size=size)
    wanted = {f"asset-{rank:04d}" for rank in range(size)}

    found = {asset.id for asset in find_assets(FakeApi(album), wanted, ["Library"])}

    assert found == wanted


def test_a_recently_taken_video_is_found_in_the_first_page() -> None:
    """Rank 0 is the newest asset, so this must not cost a library crawl."""
    album = FakeAlbum(size=9619)

    found = [asset.id for asset in find_assets(FakeApi(album), {"asset-0000"}, ["Library"])]

    assert found == ["asset-0000"]
    assert album.requests == [0]


def test_the_walk_stops_as_soon_as_the_last_wanted_asset_turns_up() -> None:
    album = FakeAlbum(size=9619)

    list(find_assets(FakeApi(album), {"asset-0150"}, ["Library"]))

    assert album.requests == [0, 100]


def test_an_id_in_no_album_costs_one_bounded_pass() -> None:
    """A stale id must not cost a crawl per id, which `_get_photo` would."""
    album = FakeAlbum(size=250)

    found = list(find_assets(FakeApi(album), {"asset-9999"}, ["Library"]))

    assert found == []
    assert album.requests == [0, 100, 200]


def test_a_missing_album_is_skipped_rather_than_raising() -> None:
    assert list(find_assets(FakeApi(None), {"asset-1"}, ["Library"])) == []


def test_the_crawl_reports_each_page_it_reads() -> None:
    """A backup whose gap is the oldest assets pages past everything it has.

    Without this the progress bar sits at zero for minutes with nothing to say
    whether the crawl is alive.
    """
    album = FakeAlbum(size=500)
    pages: list[tuple[str, int]] = []

    def on_page(album_name: str, next_offset: int) -> None:
        pages.append((album_name, next_offset))

    list(find_assets(FakeApi(album), {"asset-0450"}, ["Library"], on_page=on_page))

    assert pages == [("Library", 100), ("Library", 200), ("Library", 300), ("Library", 400)]


def test_a_resumed_crawl_starts_where_the_last_one_stopped() -> None:
    """Re-opening the session must not re-walk the library it already read."""
    album = FakeAlbum(size=9619)

    found = [
        asset.id
        for asset in find_assets(FakeApi(album), {"asset-5400"}, ["Library"], resume_from={"Library": 5300})
    ]

    assert found == ["asset-5400"]
    assert album.requests == [5300, 5400], "paid for two pages, not fifty-four"
