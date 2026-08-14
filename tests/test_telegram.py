"""Tests for src/telegram.py's alert formatting and transport."""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import requests

from src.telegram import TELEGRAM_MAX_MESSAGE_LENGTH
from src.telegram import send_enter_alert
from src.telegram import send_exit_alert
from src.telegram import send_movement_alert
from src.telegram import send_telegram_message


def _alert(**overrides) -> dict:
    base = {"id": 1, "device_name": "Steve's *Keys*", "threshold_m": 100.0, "device_icon": None}
    base.update(overrides)
    return base


@patch("src.telegram.TELEGRAM_CHAT_ID", "chat-1")
@patch("src.telegram.TELEGRAM_API_TOKEN", "token-1")
@patch("src.telegram.requests.post")
def test_send_telegram_message_posts_markdown(mock_post):
    mock_post.return_value = MagicMock(raise_for_status=MagicMock())
    send_telegram_message("*hello*")
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.telegram.org/bottoken-1/sendMessage"
    assert kwargs["data"]["chat_id"] == "chat-1"
    assert kwargs["data"]["text"] == "*hello*"
    assert kwargs["data"]["parse_mode"] == "Markdown"


@patch("src.telegram.TELEGRAM_CHAT_ID", "")
@patch("src.telegram.TELEGRAM_API_TOKEN", "")
@patch("src.telegram.requests.post")
def test_send_telegram_message_is_a_no_op_when_unconfigured(mock_post):
    send_telegram_message("*hello*")
    mock_post.assert_not_called()


@patch("src.telegram.TELEGRAM_CHAT_ID", "")
@patch("src.telegram.TELEGRAM_API_TOKEN", "")
def test_send_telegram_message_warns_when_unconfigured(caplog):
    with caplog.at_level("WARNING", logger="src.telegram"):
        send_telegram_message("*hello*")
    assert "not configured" in caplog.text


@patch("src.telegram.TELEGRAM_CHAT_ID", "chat-1")
@patch("src.telegram.TELEGRAM_API_TOKEN", "token-1")
@patch("src.telegram.requests.post")
def test_send_telegram_message_truncates_overlong(mock_post):
    mock_post.return_value = MagicMock(raise_for_status=MagicMock())
    send_telegram_message("x" * (TELEGRAM_MAX_MESSAGE_LENGTH + 50))
    text = mock_post.call_args.kwargs["data"]["text"]
    assert len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert text.endswith("...(truncated)")


@patch("src.telegram.TELEGRAM_CHAT_ID", "chat-1")
@patch("src.telegram.TELEGRAM_API_TOKEN", "token-1")
@patch("src.telegram.requests.post", side_effect=requests.RequestException("down"))
def test_send_telegram_message_propagates_transport_errors(mock_post):
    with pytest.raises(requests.RequestException):
        send_telegram_message("ping")


@patch("src.telegram.send_telegram_message")
def test_send_movement_alert_formats_name_and_distance(mock_send):
    send_movement_alert(_alert(), 250.4)
    message = mock_send.call_args.args[0]
    assert "Steve's \\*Keys\\*" in message
    assert "250m" in message
    assert "100m" in message


@patch("src.telegram.send_telegram_message")
def test_send_movement_alert_prefixes_the_devices_own_icon(mock_send):
    send_movement_alert(_alert(device_icon="🚲"), 250.4)
    assert mock_send.call_args.args[0].startswith("🚲 ")


@patch("src.telegram.send_telegram_message")
def test_send_movement_alert_falls_back_to_a_default_icon(mock_send):
    send_movement_alert(_alert(device_icon=None), 250.4)
    assert mock_send.call_args.args[0].startswith("📍 ")


@patch("src.telegram.send_telegram_message")
def test_send_enter_alert_reports_entered(mock_send):
    send_enter_alert(_alert())
    assert "entered" in mock_send.call_args.args[0]


@patch("src.telegram.send_telegram_message")
def test_send_enter_alert_prefixes_the_devices_own_icon(mock_send):
    send_enter_alert(_alert(device_icon="🔑"))
    assert mock_send.call_args.args[0].startswith("🔑 ")


@patch("src.telegram.send_telegram_message")
def test_send_exit_alert_reports_left(mock_send):
    send_exit_alert(_alert())
    assert "left" in mock_send.call_args.args[0]


@patch("src.telegram.send_telegram_message")
def test_send_exit_alert_prefixes_the_devices_own_icon(mock_send):
    send_exit_alert(_alert(device_icon="🔑"))
    assert mock_send.call_args.args[0].startswith("🔑 ")
