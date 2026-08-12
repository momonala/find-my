"""Tests for src/config.py.

These tests verify that:
1. Individual config keys return their values, in both the flag and bare forms
2. --all returns every configuration value
3. A missing or unknown key produces an error

The real `src.config.app` is exercised rather than a locally-assembled Typer
app, because the flag form (`--project-name`) depends on that app's
`ignore_unknown_options` context settings -- which is exactly what
install/install.sh relies on.
"""

import pytest
from typer.testing import CliRunner

from src.config import app

runner = CliRunner()


@pytest.mark.parametrize(
    "flag,expected_output",
    [
        ("--project-name", "test-find-my"),
        ("--project-version", "0.1.0"),
        ("--home-latitude", "52.55214"),
        ("--home-longitude", "13.39984"),
        ("--flask-port", "5016"),
    ],
)
def test_config_returns_single_value(flag: str, expected_output: str):
    result = runner.invoke(app, [flag])

    assert result.exit_code == 0
    assert result.stdout.strip() == expected_output


def test_bare_key_form_also_works():
    """install.sh uses the flag form; the underscore form is the documented alias."""
    assert runner.invoke(app, ["flask_port"]).stdout.strip() == "5016"


def test_config_all_returns_all_values():
    result = runner.invoke(app, ["--all"])

    assert result.exit_code == 0
    assert "project_name=test-find-my" in result.stdout
    assert "project_version=0.1.0" in result.stdout
    assert "flask_port=5016" in result.stdout


def test_config_without_key_fails():
    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "no config key specified" in result.output.lower()


def test_config_with_unknown_key_fails():
    result = runner.invoke(app, ["--nonsense"])

    assert result.exit_code == 1
    assert "unknown config key" in result.output.lower()
