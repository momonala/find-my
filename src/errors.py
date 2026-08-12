"""Domain errors raised by the fetch layer.

`src/find_my.py`, `src/airtags.py` and `src/tracking.py` are shared by two very
different callers: the interactive CLI (`src/cli.py`) and the Flask API's
background poller (`src/poller.py`). Raising `typer.Exit` from that shared code
would be wrong for the poller -- it runs on a daemon thread with no terminal to
exit -- so the fetch layer raises these instead and `src/cli.py` is the only
place that turns them into console output and an exit code.
"""


class FindMyError(Exception):
    """Base class for every expected, user-actionable failure in this project."""


class MissingCredentialsError(FindMyError):
    """ICLOUD_USERNAME / ICLOUD_PASSWORD aren't set in the environment."""


class LoginFailedError(FindMyError):
    """Apple rejected the username/password pair."""


class InteractiveAuthRequiredError(FindMyError):
    """Apple wants a 2FA code, but there's no terminal to prompt on.

    Raised instead of blocking forever on a stdin that nothing is attached to,
    which is what happens when the poller runs under systemd. The fix is always
    the same: run a `findmy` command once at the console to establish the
    session, then restart the service.
    """


class TwoFactorRejectedError(FindMyError):
    """The submitted 2FA code wasn't accepted."""
