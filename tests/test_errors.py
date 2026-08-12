"""Tests for the domain-error boundary between the fetch layer and the CLI.

The point of src/errors.py is that shared fetch code raises plain exceptions
instead of `typer.Exit`, so the background poller isn't reaching for a terminal
that doesn't exist. These tests pin both halves: the fetch layer raises, and
`src.cli.main` is what turns that into console output and an exit code.
"""

import pytest
import typer

import src.cli as cli
import src.tracking as tracking
from src.errors import FindMyError
from src.errors import MissingCredentialsError


def test_require_credentials_raises_a_domain_error(monkeypatch):
    monkeypatch.setattr(tracking, "ICLOUD_USERNAME", "")
    monkeypatch.setattr(tracking, "ICLOUD_PASSWORD", "")

    with pytest.raises(MissingCredentialsError):
        tracking.require_credentials()


def test_require_credentials_returns_both_when_set(monkeypatch):
    monkeypatch.setattr(tracking, "ICLOUD_USERNAME", "me@example.com")
    monkeypatch.setattr(tracking, "ICLOUD_PASSWORD", "hunter2")

    assert tracking.require_credentials() == ("me@example.com", "hunter2")


def test_domain_errors_are_not_typer_exits():
    """The whole point: the fetch layer must not raise CLI control flow.

    `typer.Exit` inherits from `click.exceptions.Exit`, which the poller's
    `except Exception` would swallow into a log line every cycle forever.
    """
    assert not issubclass(FindMyError, typer.Exit)


def test_cli_main_translates_domain_errors(monkeypatch, capsys):
    def raise_domain_error():
        raise MissingCredentialsError("Set ICLOUD_USERNAME and ICLOUD_PASSWORD in .env first.")

    monkeypatch.setattr(cli, "app", raise_domain_error)

    with pytest.raises(typer.Exit) as exit_info:
        cli.main()

    assert exit_info.value.exit_code == 1
    assert "ICLOUD_USERNAME" in capsys.readouterr().err
