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
