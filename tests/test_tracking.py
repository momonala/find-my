"""
Tests for the shared Find My tracking helpers.

These tests verify that:
1. Distance is measured from the configured home coordinates
2. Distance scales correctly in each direction (latitude vs longitude)
3. Item age is reported in minutes
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from src.config import HOME_LATITUDE
from src.config import HOME_LONGITUDE
from src.tracking import Location
from src.tracking import distance_from_home_m
from src.tracking import minutes_ago

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _location_at(latitude: float, longitude: float) -> Location:
    return Location(latitude=latitude, longitude=longitude, seen_at=_NOW)


def test_distance_at_home_is_zero():
    """The configured home coordinates are the origin of the measurement."""
    assert distance_from_home_m(_location_at(HOME_LATITUDE, HOME_LONGITUDE)) == 0


@pytest.mark.parametrize(
    "latitude_offset,longitude_offset,expected_meters",
    [
        # A thousandth of a degree of latitude is ~111m anywhere; the same step
        # of longitude covers less ground the further you are from the equator.
        (0.001, 0, 111.2),
        (0, 0.001, 67.6),
        (-0.001, 0, 111.2),
    ],
)
def test_distance_scales_by_direction(
    latitude_offset: float, longitude_offset: float, expected_meters: float
):
    location = _location_at(HOME_LATITUDE + latitude_offset, HOME_LONGITUDE + longitude_offset)

    assert distance_from_home_m(location) == pytest.approx(expected_meters, abs=0.1)


def test_minutes_ago_reports_elapsed_minutes():
    assert minutes_ago(_NOW - timedelta(seconds=90), _NOW) == pytest.approx(1.5)
