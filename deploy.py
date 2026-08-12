#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer"]
# ///
"""Deploy this project to its pi-cloud remote and sync its sqlite databases.

Run `deploy.py COMMAND --help` for a command's options and examples.
"""

import enum
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

import typer

REMOTE_HOST = "pi-cloud"
REMOTE_URL = f"mnalavadi@{REMOTE_HOST}"
PROJECT_DIR = Path(__file__).parent
INSTALL_DIR = PROJECT_DIR / "install"
LOCAL_DATA_DIR = PROJECT_DIR / "data"

# Taken from pyproject.toml rather than the local directory name, so the remote
# paths match what install/install.sh names the services and directories -- a
# local clone in a differently-named folder would otherwise point elsewhere.
with (PROJECT_DIR / "pyproject.toml").open("rb") as _handle:
    PROJECT_NAME = tomllib.load(_handle)["project"]["name"]

REMOTE_PROJECT_PATH = f"~/{PROJECT_NAME}"
REMOTE_DATA_PATH = f"/home/mnalavadi/{PROJECT_NAME}/data"
REMOTE_DATA_DIR = f"{REMOTE_URL}:{REMOTE_DATA_PATH}"
REMOTE_PROJECT_DIR = f"{REMOTE_URL}:/home/mnalavadi/{PROJECT_NAME}"

# data/ is synced separately (and much more carefully) by the `db` command - never
# blanket-overwrite the live databases as a side effect of copying code around.
COPY_EXCLUDES = [
    ".git/",
    ".venv/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".ruff_cache/",
    ".coverage",
    "data/",
    # `script(1)` session captures, which can be tens of megabytes.
    "typescript",
]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=f"Deploy {PROJECT_NAME} to pi-cloud and sync its sqlite databases. Run a command with --help.",
)


class DeployMode(str, enum.Enum):
    pull = "pull"
    fetch = "fetch"


class SyncDirection(str, enum.Enum):
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


def _restart_services_cmd(services: list[str]) -> str:
    """Build the remote shell snippet that restarts and status-checks the given services."""
    joined_services = " ".join(services)
    return f"sudo systemctl restart {joined_services} && sudo systemctl status {joined_services} --no-pager"


def _run_remote(remote_cmd: str) -> None:
    """Run `remote_cmd` on REMOTE_HOST over ssh, echoing its stdout/stderr."""
    result = _run(["ssh", REMOTE_HOST, remote_cmd], capture=True)
    typer.echo(result.stdout)
    if result.stderr:
        typer.echo(result.stderr)


@app.command()
def code(
    mode: DeployMode = typer.Argument(
        help="'pull' for a normal git pull, 'fetch' for a hard reset to origin/main (after a force-push)",
    ),
) -> None:
    r"""\[pull | fetch] Deploy code: git pull/fetch on the remote, then restart its services."""
    services = _discover_services()
    typer.secho(f"Detected {len(services)} service(s): {', '.join(services)}", fg=typer.colors.CYAN)

    if mode is DeployMode.fetch:
        git_cmd = "git fetch && git reset --hard origin/main"
        typer.secho(f"Hard reset: discarding any local changes on {REMOTE_HOST}.", fg=typer.colors.YELLOW)
    else:
        git_cmd = "git pull"

    remote_cmd = f"cd {REMOTE_PROJECT_PATH} && {git_cmd} && {_restart_services_cmd(services)}"

    typer.secho(f"Deploying {PROJECT_NAME} to {REMOTE_HOST}...", fg=typer.colors.BLUE)
    _run_remote(remote_cmd)
    typer.secho("Deployment complete.", fg=typer.colors.GREEN)


@app.command()
def restart() -> None:
    """Restart the remote's systemd services without pulling any code."""
    services = _discover_services()
    typer.secho(f"Detected {len(services)} service(s): {', '.join(services)}", fg=typer.colors.CYAN)

    typer.secho(f"Restarting {PROJECT_NAME} services on {REMOTE_HOST}...", fg=typer.colors.BLUE)
    _run_remote(_restart_services_cmd(services))
    typer.secho("Restart complete.", fg=typer.colors.GREEN)


def _discover_local_dbs() -> list[str]:
    """Return the db filenames present in local data/."""
    return sorted(p.name for p in LOCAL_DATA_DIR.glob("*.db"))


def _ssh_probe(remote_cmd: str) -> str:
    """Run a read-only probe over ssh, distinguishing "no output" from "no connection".

    `ls`/`stat` exit non-zero when the target is simply absent, which is a normal
    answer here. ssh itself exits 255 when it can't connect -- that is not an
    answer, and treating it as "nothing there" is what would let `db push`
    silently overwrite a live remote database it never actually looked at.

    Raises:
        typer.Exit: If ssh could not reach the host.
    """
    result = subprocess.run(["ssh", REMOTE_URL, remote_cmd], capture_output=True, text=True)
    if result.returncode == 255:
        typer.secho(
            f"Error: could not reach {REMOTE_URL} over ssh: {result.stderr.strip()}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    return result.stdout


def _discover_remote_dbs() -> list[str]:
    """Return the db filenames present in the remote's data/ directory."""
    output = _ssh_probe(f"ls {REMOTE_DATA_PATH}/*.db 2>/dev/null")
    return sorted(Path(line).name for line in output.splitlines() if line.strip())


def _local_mtime(path: Path) -> float | None:
    """Return the local file's mtime, or None if it doesn't exist."""
    return path.stat().st_mtime if path.exists() else None


def _remote_mtime(db_name: str) -> float | None:
    """Return the remote db's mtime, or None if it genuinely doesn't exist yet.

    A missing remote copy is an expected first-push scenario. An unreachable
    host is not, and `_ssh_probe` aborts on it rather than reporting None --
    which `_check_push_safety` would otherwise read as "safe to overwrite".
    """
    output = _ssh_probe(f"stat -c %Y '{REMOTE_DATA_PATH}/{db_name}' 2>/dev/null").strip()
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
        local_file = LOCAL_DATA_DIR / db_name
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
    direction: SyncDirection = typer.Argument(help="'pull': remote -> local. 'push': local -> remote."),
    force: bool = typer.Option(
        False, "--force", help="Push even when the remote copy is newer. Overwrites live server data."
    ),
) -> None:
    r"""\[pull | push] Sync this project's sqlite databases between local data/ and the remote.

    The pi is the authoritative writer, so push refuses when the remote copy
    is newer than local (it would silently discard data the pi has ingested
    since your last pull) unless --force is given.
    """
    if direction is SyncDirection.pull:
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
            LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
            _run(["rsync", "-avz", f"{REMOTE_DATA_DIR}/{db_name}", str(LOCAL_DATA_DIR / db_name)])
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
        _run(["rsync", "-avz", str(LOCAL_DATA_DIR / db_name), f"{REMOTE_DATA_DIR}/{db_name}"])
    typer.secho("Push complete.", fg=typer.colors.GREEN)


@app.command()
def copy(
    direction: SyncDirection = typer.Argument(help="'pull': remote -> local. 'push': local -> remote."),
) -> None:
    r"""\[pull | push] Rsync project files with the remote, bypassing git entirely.

    Useful for trying out uncommitted changes on the pi. Does not touch data/
    (see the `db` command for that) and does not restart any services - run
    `code pull` afterwards, or restart the service manually, to pick up the change.
    """
    local = f"{PROJECT_DIR}/"
    remote = f"{REMOTE_PROJECT_DIR}/"
    source, dest = (remote, local) if direction is SyncDirection.pull else (local, remote)

    typer.secho(f"Copying files {direction.value}: {source} -> {dest}", fg=typer.colors.BLUE)
    _run(["rsync", "-avz", *(f"--exclude={pattern}" for pattern in COPY_EXCLUDES), source, dest])
    typer.secho("Copy complete.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
