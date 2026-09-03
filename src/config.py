"""Non-secret configuration, read from `[tool.config]` in pyproject.toml.

Exposed both as module constants (for the app) and as a tiny CLI (`uv run
config --project-name`), which is how install/install.sh discovers the service
name and port without duplicating them in shell. Secrets live in src/env.py.
"""

import tomllib
from pathlib import Path
from typing import Any
from typing import NoReturn

import typer

_CONFIG_FILE = Path(__file__).resolve().parent.parent / "pyproject.toml"

try:
    with _CONFIG_FILE.open("rb") as handle:
        _config = tomllib.load(handle)
    _project_config = _config["project"]
    _tool_config = _config["tool"]["config"]
except (OSError, KeyError) as error:  # pragma: no cover - only on a broken checkout
    raise RuntimeError(f"Could not read [tool.config] from {_CONFIG_FILE}: {error}") from error

PROJECT_NAME = _project_config["name"]
PROJECT_VERSION = _project_config["version"]
HOME_LATITUDE = _tool_config["home_latitude"]
HOME_LONGITUDE = _tool_config["home_longitude"]
FLASK_PORT = _tool_config["flask_port"]
SPYGLASS_HOST = _tool_config["spyglass_host"]
SPYGLASS_DASHBOARD_URL = _tool_config["spyglass_dashboard_url"]

# What the CLI below exposes. Derived from the parsed tables rather than
# restated, so a new `[tool.config]` key needs no change here.
_VALUES: dict[str, Any] = {
    "project_name": PROJECT_NAME,
    "project_version": PROJECT_VERSION,
    **_tool_config,
}

app = typer.Typer(add_completion=False)


# Keys arrive as unparsed extra args, not declared options, so `_VALUES` stays
# the only place a new config key has to be registered.
@app.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def config_cli(
    ctx: typer.Context,
    show_all: bool = typer.Option(False, "--all", help="Print every key as KEY=VALUE."),
) -> None:
    """Print non-secret configuration from pyproject.toml.

    Pass a key as either `--home-latitude` or `home_latitude`. Run with --all
    to see every available key.
    """
    if show_all:
        for name, value in _VALUES.items():
            typer.echo(f"{name}={value}")
        return

    requested = [argument.lstrip("-").replace("-", "_") for argument in ctx.args]
    unknown = [name for name in requested if name not in _VALUES]
    if unknown:
        _fail(f"unknown config key(s): {', '.join(unknown)}.")
    if not requested:
        _fail("no config key specified.")

    for name in requested:
        typer.echo(_VALUES[name])


def _fail(message: str) -> NoReturn:
    typer.secho(f"Error: {message} Available keys: {', '.join(_VALUES)}.", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
