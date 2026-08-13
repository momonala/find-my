"""Telegram alerting: pushes triggered alerts (src/alerts.py) to a chat.

Optional -- TELEGRAM_API_TOKEN/TELEGRAM_CHAT_ID are blank by default, in which
case sends are silently skipped and alerting stays in-app only (the dashboard
already reads is_active/triggered_at off GET /alerts).
"""

import sqlite3

import requests

from src.env import TELEGRAM_API_TOKEN
from src.env import TELEGRAM_CHAT_ID

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def send_telegram_message(text: str) -> None:
    """Send a Markdown message to the configured Telegram chat.

    A no-op if Telegram isn't configured. Raises requests.RequestException if
    the Telegram API request fails.
    """
    if not TELEGRAM_API_TOKEN or not TELEGRAM_CHAT_ID:
        return

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _fit_telegram_length(text),
        "parse_mode": "Markdown",
    }
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_API_TOKEN}/sendMessage",
        data=payload,
    )
    response.raise_for_status()


def send_movement_alert(alert: sqlite3.Row, moved_m: float) -> None:
    """Format and send a movement-alert notification."""
    send_telegram_message(
        f"*{_escape_markdown(alert['device_name'])}* moved "
        f"{moved_m:.0f}m, over the {alert['threshold_m']:.0f}m threshold"
    )


def send_proximity_alert(alert: sqlite3.Row, *, entered: bool) -> None:
    """Format and send a proximity-alert notification for an enter/leave transition."""
    action = "entered" if entered else "left"
    send_telegram_message(
        f"*{_escape_markdown(alert['device_name'])}* {action} the "
        f"{alert['threshold_m']:.0f}m radius around home"
    )


def _fit_telegram_length(text: str) -> str:
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return text
    return text[: TELEGRAM_MAX_MESSAGE_LENGTH - 20] + "\n...(truncated)"


def _escape_markdown(text: str) -> str:
    """Escape special characters for Telegram legacy Markdown."""
    for char in ["*", "`", "["]:
        text = text.replace(char, "\\" + char)
    return text
