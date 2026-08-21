#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer"]
# ///
"""Deploy this project to its pi-cloud remote and sync its sqlite databases.

Two subcommands:
  code  git pull/fetch on the remote, then restart its systemd services.
  db    rsync the local sqlite databases with the remote's data/ directory.
"""

import enum
import subprocess
from datetime import datetime
from pathlib import Path

import typer

REMOTE_HOST = "pi-cloud"
REMOTE_URL = f"mnalavadi@{REMOTE_HOST}"
PROJECT_NAME = Path(__file__).parent.name
REMOTE_PROJECT_PATH = f"~/{PROJECT_NAME}"
INSTALL_DIR = Path(__file__).parent / "install"

REMOTE_DATA_PATH = f"/home/mnalavadi/{PROJECT_NAME}/data"
REMOTE_DATA_DIR = f"{REMOTE_URL}:{REMOTE_DATA_PATH}"

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        f"Deploy {PROJECT_NAME} to pi-cloud and sync its sqlite databases.\n\n"
        "Examples:\n\n"
        "  deploy.py code pull        git pull + restart services\n\n"
        "  deploy.py code fetch       hard reset to origin/main + restart (after a force-push)\n\n"
        "  deploy.py db pull          download databases from the remote\n\n"
        "  deploy.py db push          upload databases (refuses if the remote is newer)\n\n"
        "  deploy.py db push --force  upload anyway, overwriting the remote"
    ),
)


class DeployMode(str, enum.Enum):
    pull = "pull"
    fetch = "fetch"


class DbDirection(str, enum.Enum):
    pull = "pull"
    push = "push"


def _run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, exiting cleanly (no traceback) if it fails.

    Args:
        cmd: Argv to execute.
        capture: If True, capture stdout/stderr instead of letting them stream
            to the terminal, and echo them back on failure.

    Returns:
        The completed process on success.

    Raises:
        typer.Exit: If the command exits non-zero.
    """
    try:
        return subprocess.run(cmd, check=True, capture_output=capture, text=True)
    except subprocess.CalledProcessError as exc:
        if capture:
            typer.echo(exc.stdout)
            typer.secho(exc.stderr, fg=typer.colors.RED, err=True)
        typer.secho(
            f"Error: command failed (exit {exc.returncode}): {' '.join(cmd)}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(exc.returncode) from exc


def _discover_services() -> list[str]:
    """Return the systemd unit names for every .service file in install/."""
    services = sorted(p.stem for p in INSTALL_DIR.glob("*.service"))
    if not services:
        typer.secho(f"Error: no .service files found in {INSTALL_DIR}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    return services


@app.command()
def code(
    mode: DeployMode = typer.Argument(
        help="'pull' for a normal git pull, 'fetch' for a hard reset to origin/main (after a force-push)",
    ),
) -> None:
    """Deploy code: git pull/fetch on the remote, then restart its services."""
    services = _discover_services()
    typer.secho(f"Detected {len(services)} service(s): {', '.join(services)}", fg=typer.colors.CYAN)

    if mode is DeployMode.fetch:
        git_cmd = "git fetch && git reset --hard origin/main"
        typer.secho(f"Hard reset: discarding any local changes on {REMOTE_HOST}.", fg=typer.colors.YELLOW)
    else:
        git_cmd = "git pull"

    joined_services = " ".join(services)
    remote_cmd = (
        f"cd {REMOTE_PROJECT_PATH} && {git_cmd} && "
        f"sudo systemctl restart {joined_services} && "
        f"sudo systemctl status {joined_services} --no-pager"
    )

    typer.secho(f"Deploying {PROJECT_NAME} to {REMOTE_HOST}...", fg=typer.colors.BLUE)
    result = _run(["ssh", REMOTE_HOST, remote_cmd], capture=True)

    typer.echo(result.stdout)
    if result.stderr:
        typer.echo(result.stderr)
    typer.secho("Deployment complete.", fg=typer.colors.GREEN)


def _discover_local_dbs() -> list[str]:
    """Return the db filenames present in local data/."""
    return sorted(p.name for p in Path("data").glob("*.db"))


def _discover_remote_dbs() -> list[str]:
    """Return the db filenames present in the remote's data/ directory."""
    result = subprocess.run(
        ["ssh", REMOTE_URL, f"ls {REMOTE_DATA_PATH}/*.db 2>/dev/null"],
        capture_output=True,
        text=True,
    )
    return sorted(Path(line).name for line in result.stdout.splitlines() if line.strip())


def _local_mtime(path: Path) -> float | None:
    """Return the local file's mtime, or None if it doesn't exist."""
    return path.stat().st_mtime if path.exists() else None


def _remote_mtime(db_name: str) -> float | None:
    """Return the remote db's mtime, or None if it doesn't exist yet.

    A missing remote copy is an expected first-push scenario, not an error,
    so this intentionally skips `check=True` rather than raising on it.
    """
    result = subprocess.run(
        ["ssh", REMOTE_URL, f"stat -c %Y '{REMOTE_DATA_PATH}/{db_name}' 2>/dev/null"],
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip()
    return float(output) if output else None


def _format_local_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _check_push_safety(db_names: list[str]) -> bool:
    """Return True only if every db's local copy is at least as new as the remote's.

    The remote is assumed to be the authoritative writer, continuously
    ingesting new data. Pushing a stale local copy would silently destroy
    everything ingested since the last pull, so this refuses unless the
    local copy is newer.
    """
    safe = True
    for db_name in db_names:
        local_file = Path("data") / db_name
        local_mtime = _local_mtime(local_file)
        if local_mtime is None:
            typer.secho(f"✗ {db_name}: no local copy at {local_file}.", fg=typer.colors.RED, err=True)
            safe = False
            continue

        remote_mtime = _remote_mtime(db_name)
        if remote_mtime is None:
            typer.secho(f"! {db_name}: no remote copy — will create it.", fg=typer.colors.YELLOW)
        elif remote_mtime > local_mtime:
            typer.secho(f"✗ {db_name}: remote is NEWER than local.", fg=typer.colors.RED, err=True)
            typer.echo(f"  remote: {_format_local_time(remote_mtime)}", err=True)
            typer.echo(f"  local:  {_format_local_time(local_mtime)}", err=True)
            safe = False
        else:
            typer.secho(
                f"✓ {db_name}: local is newer ({_format_local_time(local_mtime)}).", fg=typer.colors.GREEN
            )
    return safe


@app.command()
def db(
    direction: DbDirection = typer.Argument(help="'pull': remote -> local. 'push': local -> remote."),
    force: bool = typer.Option(
        False, "--force", help="Push even when the remote copy is newer. Overwrites live server data."
    ),
) -> None:
    """Sync this project's sqlite databases between local data/ and the remote.

    The pi is the authoritative writer, so push refuses when the remote copy
    is newer than local (it would silently discard data the pi has ingested
    since your last pull) unless --force is given.
    """
    if direction is DbDirection.pull:
        db_names = _discover_remote_dbs()
        if not db_names:
            typer.secho(
                f"Error: no .db files found on remote at {REMOTE_DATA_PATH}", fg=typer.colors.RED, err=True
            )
            raise typer.Exit(1)
        typer.secho(
            f"Detected {len(db_names)} database(s) on remote: {', '.join(db_names)}", fg=typer.colors.CYAN
        )

        for db_name in db_names:
            typer.secho(f"Pulling {db_name} from remote...", fg=typer.colors.BLUE)
            _run(["rsync", "-avz", f"{REMOTE_DATA_DIR}/{db_name}", f"data/{db_name}"])
        typer.secho("Pull complete.", fg=typer.colors.GREEN)
        return

    db_names = _discover_local_dbs()
    if not db_names:
        typer.secho("Error: no .db files found in local data/", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho(f"Detected {len(db_names)} database(s) locally: {', '.join(db_names)}", fg=typer.colors.CYAN)

    typer.secho("Checking remote freshness...", fg=typer.colors.BLUE)
    if not _check_push_safety(db_names):
        if not force:
            typer.echo("", err=True)
            typer.secho(
                "Push aborted: the remote has data your local copy does not.", fg=typer.colors.RED, err=True
            )
            typer.secho(
                "Pull first, or re-run with --force to overwrite the server.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(1)
        typer.secho("--force given: overwriting newer remote data anyway.", fg=typer.colors.YELLOW)

    for db_name in db_names:
        typer.secho(f"Pushing {db_name} to remote...", fg=typer.colors.BLUE)
        _run(["rsync", "-avz", f"data/{db_name}", f"{REMOTE_DATA_DIR}/{db_name}"])
    typer.secho("Push complete.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
