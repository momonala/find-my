"""Telegram alerting: pushes triggered alerts (src/findmy/alerts.py) to a chat.

Optional -- TELEGRAM_API_TOKEN/TELEGRAM_CHAT_ID are blank by default, in which
case sends are skipped (logged as a warning) and alerting stays in-app only:
the dashboard reads is_active/triggered_at off GET /alerts, and surfaces the
same "not configured" state via GET /config's telegram_configured field.
"""

import logging
import sqlite3

import requests

from src.core.env import TELEGRAM_API_TOKEN
from src.core.env import TELEGRAM_CHAT_ID
from src.core.telemetry import metrics

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_TRUNCATION_SUFFIX = "\n...(truncated)"
# Prefix for a device with no custom marker set (src/findmy/db.py's device_icons
# table), so every alert stays visually scannable in the chat.
DEFAULT_ALERT_ICON = "📍"


def send_telegram_message(text: str) -> None:
    """Send a Markdown message to the configured Telegram chat.

    A no-op (aside from a warning log) if Telegram isn't configured. Raises
    requests.RequestException if the Telegram API request fails.
    """
    if not TELEGRAM_API_TOKEN or not TELEGRAM_CHAT_ID:
        metrics.increment("telegram_skipped")
        logger.warning(
            "Telegram not configured (TELEGRAM_API_TOKEN/TELEGRAM_CHAT_ID unset); "
            "dropping notification: %s",
            text,
        )
        return

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_API_TOKEN}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": _fit_telegram_length(text),
            "parse_mode": "Markdown",
        },
    )
    response.raise_for_status()


def _alert_prefix(alert: sqlite3.Row) -> str:
    """The alerted device's marker emoji and name, as every message opens."""
    icon = alert["device_icon"] or DEFAULT_ALERT_ICON
    return f"{icon} *{_escape_markdown(alert['device_name'])}*"


def send_movement_alert(alert: sqlite3.Row, moved_m: float) -> None:
    """Format and send a movement-alert notification."""
    send_telegram_message(
        f"{_alert_prefix(alert)} moved {moved_m:.0f}m, over the {alert['threshold_m']:.0f}m threshold"
    )


def send_enter_alert(alert: sqlite3.Row) -> None:
    """Format and send an enter-radius-alert notification."""
    send_telegram_message(
        f"{_alert_prefix(alert)} entered the {alert['threshold_m']:.0f}m radius around home"
    )


def send_exit_alert(alert: sqlite3.Row) -> None:
    """Format and send a leave-radius-alert notification."""
    send_telegram_message(f"{_alert_prefix(alert)} left the {alert['threshold_m']:.0f}m radius around home")


def _fit_telegram_length(text: str) -> str:
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return text
    return text[: TELEGRAM_MAX_MESSAGE_LENGTH - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


def _escape_markdown(text: str) -> str:
    """Escape special characters for Telegram legacy Markdown."""
    for char in ("*", "`", "["):
        text = text.replace(char, "\\" + char)
    return text
