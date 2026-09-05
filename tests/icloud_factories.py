"""Builders for the iCloud backup tests.

Kept out of `conftest.py`, which describes the Find My app's domain: these
shapes belong to the optional photos subsystem and are imported explicitly by
the tests that need them.
"""

from datetime import UTC
from datetime import datetime

from icloud_photo_metadata.models import PhotoMetadata
from icloud_photo_metadata.models import StoredResource

# Fixed so anything deriving an ordering or a capture year is deterministic.
CAPTURED_AT = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


def make_photo(
    photo_id: str,
    filename: str,
    size: int = 1000,
    *,
    album: str = "Library",
    item_type: str = "image",
    live_size: int | None = None,
    captured_at: datetime = CAPTURED_AT,
) -> PhotoMetadata:
    """One iCloud asset. `live_size` gives it a Live Photo motion clip."""
    resources = {"original": StoredResource(size_bytes=size, content_type="public.heic")}
    if live_size is not None:
        resources["original_video"] = StoredResource(
            size_bytes=live_size, content_type="com.apple.quicktime-movie"
        )
    return PhotoMetadata(
        id=photo_id,
        master_id=f"master-{photo_id}",
        filename=filename,
        source_album=album,
        item_type=item_type,
        is_live_photo=live_size is not None,
        resources=resources,
        total_storage_bytes=size + (live_size or 0),
        asset_date=captured_at,
        added_date=captured_at,
        is_favorite=False,
        is_hidden=False,
    )
