"""Telegram alerting: pushes triggered alerts (src/alerts.py) to a chat.

Optional -- TELEGRAM_API_TOKEN/TELEGRAM_CHAT_ID are blank by default, in which
case sends are skipped (logged as a warning) and alerting stays in-app only
(the dashboard already reads is_active/triggered_at off GET /alerts, and
surfaces the same "not configured" state -- see GET /config's
telegram_configured field).
"""

import logging
import sqlite3

import requests

from src.env import TELEGRAM_API_TOKEN
from src.env import TELEGRAM_CHAT_ID
from src.telemetry import metrics

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
# Prefix for a device with no custom marker set (src/db.py's device_icons
# table) -- keeps every alert visually scannable in the chat, not just the
# ones for devices someone bothered to give an emoji.
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


def _alert_icon(alert: sqlite3.Row) -> str:
    """The alerted device's own marker emoji (set via PUT .../icon), or a
    generic pin if it doesn't have one."""
    return alert["device_icon"] or DEFAULT_ALERT_ICON


def send_movement_alert(alert: sqlite3.Row, moved_m: float) -> None:
    """Format and send a movement-alert notification."""
    send_telegram_message(
        f"{_alert_icon(alert)} *{_escape_markdown(alert['device_name'])}* moved "
        f"{moved_m:.0f}m, over the {alert['threshold_m']:.0f}m threshold"
    )


def send_proximity_alert(alert: sqlite3.Row, *, entered: bool) -> None:
    """Format and send a proximity-alert notification for an enter/leave transition."""
    action = "entered" if entered else "left"
    send_telegram_message(
        f"{_alert_icon(alert)} *{_escape_markdown(alert['device_name'])}* {action} the "
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
