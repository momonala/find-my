"""Domain errors raised by the fetch layer.

The fetch modules are shared by the interactive CLI and the API's background
poller, so they can't raise `typer.Exit` -- the poller runs on a daemon thread
with no terminal to exit. They raise these instead, and src/cli.py is the only
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


class UnsupportedPlatformError(FindMyError):
    """The operation requires macOS 14+.

    AirTag key refresh reads Keychain and Find My library paths that don't
    exist elsewhere, and initial Anisette setup downloads Apple-native binaries
    that only run on macOS 14+. Pre-seed .icloud_session/ and trackers.json
    from a Mac instead.
    """
