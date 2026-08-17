"""Secrets loaded from .env (copy .env.example to .env for local development)."""

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME", "")
ICLOUD_PASSWORD = os.environ.get("ICLOUD_PASSWORD", "")

# Optional shared secret for the API's one write route (PUT /locations/<id>/icon).
# Unset leaves writes open, which is fine for the localhost default; set it
# before exposing the dashboard on a network or through a tunnel.
API_WRITE_TOKEN = os.environ.get("API_WRITE_TOKEN", "")

# Optional Telegram bot used to push alert notifications (src/telegram.py).
# Unset leaves alerting in-app only -- the dashboard still shows triggered
# alerts via GET /alerts.
TELEGRAM_API_TOKEN = os.environ.get("TELEGRAM_API_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Optional MapTiler key that unlocks extra, more-reliable tile options in the
# dashboard's map style picker (see GET /config). Unset keeps the free
# CARTO/OSM raster styles only.
MAPTILER_API_KEY = os.environ.get("MAPTILER_API_KEY", "")
