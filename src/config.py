"""Non-secret configuration, read from `[tool.config]` in pyproject.toml.

Exposed both as module constants (for the app) and as a tiny CLI (`uv run
config --project-name`), which is how install/install.sh discovers the service
name and port without duplicating them in shell. Secrets live in src/env.py.
"""

import tomllib
from pathlib import Path
from typing import Any

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

# The single source of truth for the CLI below: one entry per exposed key, so
# adding a config value means editing this dict and nothing else.
_VALUES: dict[str, Any] = {
    "project_name": PROJECT_NAME,
    "project_version": PROJECT_VERSION,
    "home_latitude": HOME_LATITUDE,
    "home_longitude": HOME_LONGITUDE,
    "flask_port": FLASK_PORT,
}

app = typer.Typer(add_completion=False)


# Keys arrive as unparsed extra args rather than declared options so that this
# stays driven by `_VALUES` alone -- declaring one boolean flag per key means
# every new config value has to be added in two places and kept in sync.
@app.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def config_cli(
    ctx: typer.Context,
    show_all: bool = typer.Option(False, "--all", help="Print every key as KEY=VALUE."),
) -> None:
    """Print non-secret configuration from pyproject.toml.

    Pass a key as either `--home-latitude` or `home_latitude`. Available keys:
    project_name, project_version, home_latitude, home_longitude, flask_port.
    """
    if show_all:
        for name, value in _VALUES.items():
            typer.echo(f"{name}={value}")
        return

    requested = [argument.lstrip("-").replace("-", "_") for argument in ctx.args]
    unknown = [name for name in requested if name not in _VALUES]

    if requested and not unknown:
        for name in requested:
            typer.echo(_VALUES[name])
        return

    if unknown:
        message = f"Error: unknown config key(s): {', '.join(unknown)}."
    else:
        message = "Error: no config key specified."
    typer.secho(f"{message} Available keys: {', '.join(_VALUES)}.", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
