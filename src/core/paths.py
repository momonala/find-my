"""Filesystem locations shared across the app.

Resolved once here rather than per module: the number of `.parent` hops up to
the repo root differs by nesting depth, and getting it wrong fails at import
time looking like a missing file.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"
# Apple session cookies and tracker keys. Shared with the media tooling, which
# borrows an already-trusted session rather than prompting for 2FA again.
SESSION_DIR = REPO_ROOT / ".icloud_session"
