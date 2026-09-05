"""The command line for the backup tool.

Every `console.print` in this package lives here; the pipeline returns plans and
outcomes. Nothing is configurable -- a flag would only be a way to get a
different answer to the same question.

Usage:
    uv run --group photos python -m icloud_photo_metadata plan
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from icloud_photo_metadata import library
from icloud_photo_metadata import migrate_manifest
from icloud_photo_metadata.backup import DEFAULT_DIR
from icloud_photo_metadata.backup import MAX_FRUITLESS_REFRESHES
from icloud_photo_metadata.backup import BackupIndex
from icloud_photo_metadata.backup import SyncPlan
from icloud_photo_metadata.backup import apply_plan
from icloud_photo_metadata.backup import build_index
from icloud_photo_metadata.backup import build_plan
from icloud_photo_metadata.backup import eligible
from icloud_photo_metadata.drive import scan
from icloud_photo_metadata.drive import sha256
from icloud_photo_metadata.icloud_source import authenticate
from icloud_photo_metadata.models import PhotoMetadata
from icloud_photo_metadata.pull_metadata import refresh_incremental

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()

_UNIT_STEP = 1000.0


@app.callback()
def _root() -> None:
    """Back up the iCloud media library to an external drive."""


def format_bytes(num_bytes: float) -> str:
    """Render a byte count in decimal units, matching how Apple reports storage."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < _UNIT_STEP:
            return f"{size:.1f} {unit}"
        size /= _UNIT_STEP
    return f"{size:.1f} TB"


def _require_drive(directory: Path) -> None:
    if not directory.is_dir():
        typer.echo(f"{directory} is not a directory -- is the drive mounted?", err=True)
        raise typer.Exit(1)


def _metadata_age(conn: sqlite3.Connection) -> str:
    """How stale the library's view of iCloud is, in words."""
    pulled = library.pulled_at(conn)
    if pulled is None:
        return "iCloud has never been pulled"
    age = datetime.now(UTC) - pulled
    hours = age.total_seconds() / 3600
    return f"the metadata is {hours:.0f}h old" if hours < 48 else f"the metadata is {age.days} days old"


def _refresh_metadata(conn: sqlite3.Connection, api: Any) -> None:
    """Re-pull the metadata, refusing to plan against a partial view.

    A pull that lost pages could be missing whole assets, and a download
    reporting success against it would claim a backup it never made.
    """
    result = refresh_incremental(conn, api)
    if not result.is_complete:
        typer.echo(
            f"{result.failed_pages} metadata pages failed after retries; the library was left "
            "unchanged and nothing was downloaded. Re-run to try again.",
            err=True,
        )
        raise typer.Exit(1)
    console.print(f"Pulled metadata for {result.written:,} iCloud items.")


def _print_plan(plan: SyncPlan, index: BackupIndex, directory: Path) -> None:
    summary = Table(title=f"Sync plan for {directory}")
    summary.add_column("Operation")
    summary.add_column("Files", justify="right")
    summary.add_column("Size", justify="right")
    summary.add_row("already backed up", f"{len(index.recorded):,}", "-")
    if plan.downloads:
        live = sum(1 for download in plan.downloads if download.live_destination)
        label = "download" + (f" ({live:,} with a Live Photo clip)" if live else "")
        summary.add_row(label, f"{len(plan.downloads):,}", format_bytes(plan.download_bytes))
    console.print(summary)

    if index.stray:
        console.print(
            f"\n[yellow]{len(index.stray):,} file(s) here belong to no library record. This "
            "directory should hold only what a sync put in it:[/yellow]"
        )
        for path in index.stray[:20]:
            console.print(f"  {path.relative_to(directory)}")


@dataclass(frozen=True)
class _Survey:
    """What one look at iCloud and the drive found."""

    plan: SyncPlan
    index: BackupIndex
    api: Any
    # Not just the eligible ones: the comparison needs Hidden and Recently
    # Deleted to tell a freed asset from a hidden one.
    in_icloud: list[PhotoMetadata]


def _survey(conn: sqlite3.Connection) -> _Survey:
    """Refresh the metadata, then work out what the drive is missing.

    Shared by plan and download so the two cannot disagree. The refresh is not
    optional: a plan built from whatever the library last held could report the
    drive matches iCloud having never asked iCloud anything.
    """
    api = authenticate()
    _refresh_metadata(conn, api)

    # A freed asset cannot be downloaded, so counting it missing would put it
    # in every plan forever.
    in_icloud = library.load_assets(conn, in_icloud_only=True)
    photos = eligible(in_icloud)
    files = scan(DEFAULT_DIR)
    console.print(f"iCloud assets: {len(photos):,}   files on drive: {len(files):,}\n")

    index = build_index(photos, files, library.backed_up(conn), DEFAULT_DIR)
    return _Survey(build_plan(index, DEFAULT_DIR), index, api, in_icloud)


_NOTHING_TO_DO = "\n[green]Nothing to do -- the drive already matches iCloud.[/green]"


@app.command("plan")
def plan_command() -> None:
    """Report what a download would fetch, without fetching anything."""
    _require_drive(DEFAULT_DIR)
    with library.library(DEFAULT_DIR) as conn:
        survey = _survey(conn)
        _print_plan(survey.plan, survey.index, DEFAULT_DIR)
        _print_icloud_comparison(conn, survey.in_icloud)
        if survey.plan.is_empty:
            console.print(_NOTHING_TO_DO)


@app.command("download")
def download_command() -> None:
    """Download every asset the drive is missing.

    Exits non-zero if the metadata pull was incomplete or any download failed.
    """
    _require_drive(DEFAULT_DIR)
    with library.library(DEFAULT_DIR) as conn:
        survey = _survey(conn)
        _print_plan(survey.plan, survey.index, DEFAULT_DIR)
        if survey.plan.is_empty:
            console.print(_NOTHING_TO_DO)
            return

        outcome = apply_plan(survey.plan, DEFAULT_DIR, conn, api=survey.api)
        for filename in outcome.unfetched:
            console.print(f"  [yellow]! {filename}: not found in any album[/yellow]")
        if outcome.aborted:
            console.print(
                f"\n[red]Gave up: {MAX_FRUITLESS_REFRESHES} fresh sessions in a row downloaded "
                f"nothing, with {outcome.remaining:,} to go. What did land is recorded; re-run to "
                "continue.[/red]"
            )

        reopened = outcome.sessions_reopened
        refreshed = f", re-opened the session {reopened}x" if reopened else ""
        console.print(
            f"\n[green]Downloaded {outcome.downloaded:,}[/green], failed {outcome.failed:,}{refreshed}."
        )
        if outcome.failed:
            raise typer.Exit(1)


@app.command("verify")
def verify_command() -> None:
    """Confirm every file the library claims is present and byte-for-byte intact.

    Run this before deleting originals from iCloud. It hashes the whole drive
    every time, because a size check cannot catch a file that rots without
    resizing.

    A clean run refreshes `library.db.dump`; a failed one must not, or it would
    overwrite the copy taken while the library was whole.

    Exits non-zero only on damage to the backup itself: an asset not backed up
    yet is `download`'s business.
    """
    _require_drive(DEFAULT_DIR)
    with library.library(DEFAULT_DIR) as conn:
        backed_up = library.backed_up(conn)
        if not backed_up:
            typer.echo(f"No files recorded in {DEFAULT_DIR / library.LIBRARY_NAME}.", err=True)
            raise typer.Exit(1)

        damage: list[str] = []
        files = 0
        for asset in sorted(backed_up.values(), key=lambda item: item.files[0].relative_path):
            for item in asset.files:
                files += 1
                if problem := _damage_at(DEFAULT_DIR / item.relative_path, item):
                    damage.append(problem)

        console.print(f"The library records {len(backed_up):,} assets in {files:,} files.")
        if damage:
            console.print(f"[red]{len(damage)} problem(s):[/red]")
            for problem in damage[:40]:
                console.print(f"  {problem}")
        else:
            console.print(f"[green]All {files:,} present and verified byte-for-byte.[/green]")
            written = library.write_dump(conn, DEFAULT_DIR)
            console.print(f"Wrote {library.DUMP_NAME} ({format_bytes(written)}).")

        _print_icloud_comparison(conn, library.load_assets(conn, in_icloud_only=True))
        if damage:
            raise typer.Exit(1)


@app.command("import-manifest")
def import_manifest_command() -> None:
    """Move a pre-database `.backup-manifest.json` into the library, once.

    Refuses to call it done unless every record arrived intact. The JSON is left
    where it is: delete it once a `verify` has passed.
    """
    _require_drive(DEFAULT_DIR)
    with library.library(DEFAULT_DIR) as conn:
        try:
            result = migrate_manifest.import_manifest(conn, DEFAULT_DIR)
        except FileNotFoundError as error:
            typer.echo(f"{error} Nothing to import.", err=True)
            raise typer.Exit(1) from error

        console.print(
            f"Imported {result.records:,} records covering {result.files:,} files; "
            f"{result.restored:,} were assets only the manifest knew about."
        )
        if not result.is_clean:
            console.print(f"[red]{len(result.discrepancies)} record(s) did not survive:[/red]")
            for problem in result.discrepancies[:40]:
                console.print(f"  {problem}")
            raise typer.Exit(1)
        console.print(
            "[green]Every record checked out.[/green] Run verify, then delete "
            f"{migrate_manifest.MANIFEST_NAME}."
        )


def _damage_at(path: Path, item: library.BackedUpFile) -> str | None:
    """What is wrong with one file the library claims, or None if nothing is."""
    if not path.exists():
        return f"missing: {item.relative_path}"
    if path.stat().st_size != item.size_bytes:
        return f"size changed: {item.relative_path}"
    if sha256(path) != item.sha256:
        return f"hash mismatch: {item.relative_path}"
    return None


def _print_icloud_comparison(conn: sqlite3.Connection, in_icloud: list[PhotoMetadata]) -> None:
    """Say how the drive's records line up with the library's view of iCloud.

    Backup-only assets are the point of the exercise, so they are stated plainly
    rather than reported as a discrepancy.
    """
    backed_up = library.backed_up(conn)
    backup_only = set(backed_up) - {photo.id for photo in in_icloud}
    unbacked = {photo.id for photo in eligible(in_icloud)} - set(backed_up)

    console.print(f"\nAgainst iCloud, as of the last pull ({_metadata_age(conn)}):")
    console.print(f"  [green]{len(backed_up) - len(backup_only):,}[/green] still in iCloud and backed up")
    if backup_only:
        freed = sum(backed_up[asset_id].total_bytes for asset_id in backup_only)
        console.print(
            f"  [cyan]{len(backup_only):,}[/cyan] backup-only, no longer in iCloud "
            f"({format_bytes(freed)} this drive is the only copy of)"
        )
    if unbacked:
        console.print(f"  [yellow]{len(unbacked):,}[/yellow] iCloud assets not backed up yet -- run download")
