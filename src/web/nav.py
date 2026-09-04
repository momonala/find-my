"""The pages the shell offers.

The only place the set of pages is written down: base.html's nav and the
landing page both render whatever this lists. See "Adding a page" in
README.md for the rest of the steps.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    """One page in the shell.

    A view passes its `slug` as `active_page` to mark itself current, and
    `icon` names one of the SVGs in templates/_icons.html.
    """

    slug: str
    label: str
    endpoint: str
    summary: str
    icon: str


NAV_ITEMS = (
    NavItem(
        slug="findmy",
        label="Find My",
        endpoint="findmy.page",
        icon="pin",
        summary="Devices and AirTags on a live map, with movement and geofence alerts.",
    ),
)
