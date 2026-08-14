"""Single entry point for Find My lookups: `uv run findmy <command>`.

Presentation lives here; src/find_my.py and src/airtags.py just return
`list[TrackedItem]`, so every command shares the same sorting and output paths.
"""

import logging
import threading
import time
from collections.abc import Callable

import typer

from src.airtags import fetch_airtags
from src.airtags import has_cached_keys
from src.airtags import is_macos_14_plus
from src.airtags import load_trackers
from src.config import FLASK_PORT
from src.errors import FindMyError
from src.find_my import fetch_devices
from src.tracking import SortKey
from src.tracking import TrackedItem
from src.tracking import items_to_json
from src.tracking import render_items
from src.tracking import sort_items

app = typer.Typer(help="Locate iCloud devices and AirTags.", no_args_is_help=True)

_SortOption = typer.Option(SortKey.DISTANCE, "--sort", help="Order results by this field.")
_JsonOption = typer.Option(False, "--json", help="Emit JSON instead of a table.")
_RefreshOption = typer.Option(
    False, "--refresh-keys", help="Re-read tracker keys from the Keychain, e.g. after pairing one."
)


def _report(fetch: Callable[[], list[TrackedItem]], title: str, sort: SortKey, as_json: bool) -> None:
    """Time a fetch, then print its items as JSON or a table."""
    started = time.perf_counter()
    items = sort_items(fetch(), sort)
    elapsed = time.perf_counter() - started

    if as_json:
        typer.echo(items_to_json(items))
        return
    if not items:
        typer.secho(f"No {title.lower()} found.", fg=typer.colors.YELLOW)
        return
    render_items(items, title=title, elapsed_seconds=elapsed)


@app.command("devices")
def devices_command(sort: SortKey = _SortOption, as_json: bool = _JsonOption) -> None:
    """Locate Apple devices signed into iCloud (iPhones, iPads, Macs, AirPods)."""
    _report(fetch_devices, title="Apple devices", sort=sort, as_json=as_json)


@app.command("airtags")
def airtags_command(
    sort: SortKey = _SortOption,
    as_json: bool = _JsonOption,
    refresh_keys: bool = _RefreshOption,
) -> None:
    """Locate AirTags and other Find My network trackers paired to this Mac."""
    _require_macos_for_keychain(refresh_keys)
    _warn_if_keychain_needed(refresh_keys)
    _report(lambda: fetch_airtags(refresh_keys), title="AirTags", sort=sort, as_json=as_json)


@app.command("all")
def all_command(
    sort: SortKey = _SortOption,
    as_json: bool = _JsonOption,
    refresh_keys: bool = _RefreshOption,
) -> None:
    """Locate everything: devices and trackers in one table."""
    _require_macos_for_keychain(refresh_keys)
    _warn_if_keychain_needed(refresh_keys)
    _report(
        lambda: fetch_devices() + fetch_airtags(refresh_keys),
        title="Find My items",
        sort=sort,
        as_json=as_json,
    )


@app.command("serve")
def serve_command(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind the API to."),
    port: int = typer.Option(FLASK_PORT, "--port", help="Port to bind the API to."),
    poll: bool = typer.Option(
        True,
        "--poll/--no-poll",
        help="Run the fetch loop in-process. Use --no-poll when `findmy poll` runs separately.",
    ),
) -> None:
    """Serve the read-only Find My API and dashboard.

    By default this also runs the fetch loop in-process, which is what you want
    on a single machine. Deployments that run more than one web worker should
    pass --no-poll and run `findmy poll` as its own service, so exactly one
    process writes to the database.
    """
    from src.api import create_app

    _configure_server_logging()
    create_app(start_poller=poll).run(host=host, port=port)


@app.command("poll")
def poll_command() -> None:
    """Run only the background fetch loop, writing to the shared database.

    The counterpart to `findmy serve --no-poll`. Needs a warm .icloud_session/,
    so run `findmy airtags` once at the console first.
    """
    from src.db import init_db
    from src.poller import run_forever

    _configure_server_logging()
    init_db()
    stop_event = threading.Event()
    try:
        run_forever(stop_event)
    except KeyboardInterrupt:
        stop_event.set()


@app.command("refresh-keys")
def refresh_keys_command() -> None:
    """Re-read tracker keys from the Keychain without locating anything."""
    _require_macos_for_keychain(refresh_keys=True)
    _warn_if_keychain_needed(refresh_keys=True)
    trackers = load_trackers(refresh_keys=True)
    typer.secho(f"Cached keys for {len(trackers)} trackers.", fg=typer.colors.GREEN)


def _configure_server_logging() -> None:
    """Set up console logging for the long-running server commands.

    This lives here rather than in `create_app` so that building the app -- in a
    test, say -- doesn't reconfigure logging for the whole process.
    """
    # Without a configured handler, logger.info() calls (e.g. src.poller's
    # once-a-minute fetch log) are silently dropped -- INFO is below the
    # logging module's default "handler of last resort" level (WARNING).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Werkzeug logs every request at INFO by default, which drowns out
    # anything else on the console; only its warnings/errors matter here.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # pyicloud logs its own "Number of devices found" line every poll cycle,
    # duplicating src.poller's single per-cycle summary; only its
    # warnings/errors matter here.
    logging.getLogger("pyicloud").setLevel(logging.WARNING)


def _require_macos_for_keychain(refresh_keys: bool) -> None:
    """Exit with a clear error if a Keychain operation is requested on a non-Mac."""
    if (refresh_keys or not has_cached_keys()) and not is_macos_14_plus():
        typer.secho(
            "Tracker key operations require macOS 14+. "
            "Run this command on the Mac, then copy .icloud_session/ and trackers.json here.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)


def _warn_if_keychain_needed(refresh_keys: bool) -> None:
    if refresh_keys or not has_cached_keys():
        typer.echo("Reading tracker keys from this Mac's Find My data (may prompt for Keychain access)...")


def main() -> None:
    # findmy logs a line per accessory missing optional local records, which is
    # normal for trackers that haven't reported in a while, and only noise here.
    logging.getLogger("findmy").setLevel(logging.ERROR)
    try:
        app()
    except FindMyError as error:
        # The one place domain errors become console output and an exit code;
        # see src/errors.py for why the fetch layer doesn't do this itself.
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from error


if __name__ == "__main__":
    main()
