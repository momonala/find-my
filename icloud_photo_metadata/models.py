"""What one iCloud Photos asset is, independent of where it is stored.

`icloud_source.py` builds these from CloudKit records and `library.py` writes
them to the drive; nothing here knows about either.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator


class StoredResource(BaseModel):
    """One stored variant of an asset: original, preview, thumbnail, live video.

    Every variant occupies iCloud quota, which is why `total_storage_bytes`
    sums all of them rather than the original alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    size_bytes: int | None = None
    content_type: str | None = None
    width: int | None = None
    height: int | None = None
    # Apple's content hash for these exact bytes, not reproducible locally.
    checksum: str | None = None


class GpsFix(BaseModel):
    """Where a photo was taken, decoded from the asset's `locationEnc` field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float
    longitude: float
    altitude: float | None = None
    speed: float | None = None
    timestamp: datetime | None = None

    @field_validator("timestamp")
    @classmethod
    def _as_utc(cls, value: datetime | None) -> datetime | None:
        """GPS time is UTC by definition, but Apple's decoder drops the zone.

        Left naive it is the one datetime on the asset that cannot be compared
        with the others.
        """
        return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class PhotoMetadata(BaseModel):
    """Everything one iCloud Photos asset exposes, minus the pixels.

    `extra="forbid"`: a record written by an older schema fails loudly instead
    of reading as a pile of `None`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    master_id: str
    filename: str
    source_album: str

    item_type: str
    is_live_photo: bool
    asset_subtype_v2: int | None = None
    hdr_type: int | None = None
    burst_flags: int | None = None
    data_class_type: int | None = None

    resources: dict[str, StoredResource]
    total_storage_bytes: int

    asset_date: datetime  # Photos' canonical capture date.
    added_date: datetime
    original_creation_date: datetime | None = None
    import_date: datetime | None = None
    master_modified: datetime | None = None
    asset_modified: datetime | None = None
    time_zone_name: str | None = None
    time_zone_offset_seconds: int | None = None

    duration_seconds: float | None = None  # Normalized from Apple's milliseconds.
    video_frame_rate: float | None = None
    orientation: int | None = None
    original_orientation: int | None = None

    is_favorite: bool
    is_hidden: bool
    rating: int | None = None
    title: str | None = None
    description: str | None = None
    keywords: list[str] | None = None
    people: list[Any] = Field(default_factory=list)
    view_count: int | None = None
    play_count: int | None = None
    share_count: int | None = None

    imported_by_bundle_id: str | None = None
    imported_by_display_name: str | None = None

    location: GpsFix | None = None
    # Raw EXIF/TIFF/GPS plist, its shape varying by camera.
    exif: dict[str, Any] | None = None

    @property
    def original(self) -> StoredResource | None:
        return self.resources.get("original")

    @property
    def original_size_bytes(self) -> int | None:
        return self.original.size_bytes if self.original else None

    @property
    def live_video(self) -> StoredResource | None:
        """A Live Photo's motion clip. Absent on stills and on movie assets."""
        return self.resources.get("original_video")

    @property
    def live_video_size_bytes(self) -> int | None:
        return self.live_video.size_bytes if self.live_video else None
