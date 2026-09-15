"""Secrets loaded from .env (copy .env.example to .env for local development)."""

import os

from dotenv import load_dotenv

from src.core.paths import REPO_ROOT

load_dotenv(REPO_ROOT / ".env")

ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME", "")
ICLOUD_PASSWORD = os.environ.get("ICLOUD_PASSWORD", "")

# Optional shared secret gating the API's write routes (icon and alert config).
# Unset leaves writes open, which is fine for the localhost default; set it
# before exposing the dashboard on a network or through a tunnel.
API_WRITE_TOKEN = os.environ.get("API_WRITE_TOKEN", "")

# Optional Telegram bot used to push alert notifications (src/findmy/telegram.py).
# Unset leaves alerting in-app only -- the dashboard still shows triggered
# alerts via GET /alerts.
TELEGRAM_API_TOKEN = os.environ.get("TELEGRAM_API_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Optional MapTiler key that unlocks extra, more-reliable tile options in the
# dashboard's map style picker (see GET /config). Unset keeps the free
# CARTO/OSM raster styles only.
MAPTILER_API_KEY = os.environ.get("MAPTILER_API_KEY", "")

# Optional Google Cloud key for the Map Tiles API, which adds Google's styles to
# the dashboard's picker. Unlike MAPTILER_API_KEY it is never sent to the
# browser; src/maps/google_tiles.py explains why. Unset hides those styles.
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
