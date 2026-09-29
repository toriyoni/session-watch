"""Typed UCAS errors with a machine-readable code and request stage.

Front ends report UcasError subclasses at a command/pass boundary and keep
running. UcasNotImplementedError distinguishes an unknown upstream shape from
ordinary network/authentication failures so it remains visible in logs.
Unexpected programming errors are not swallowed by these handlers.
"""

from __future__ import annotations


class UcasError(Exception):
    """Base class for every deliberate error raised by this package."""

    def __init__(
        self,
        message: str,
        code: str = "UNEXPECTED_ERROR",
        stage: str = "request",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.stage = stage

    def describe(self) -> str:
        """Message plus code and stage, for log lines and notifications."""
        return f"{self.message} (code={self.code}, stage={self.stage})"


class UcasOperationalError(UcasError):
    """Base class for errors the front ends survive.

    Catching this (and only this) means: log it, notify the user, carry on.
    """


class UcasTimeError(UcasOperationalError):
    """The date/time value handed in is unusable (``BAD_DATE``)."""


class UcasNetworkError(UcasOperationalError):
    """The HTTP request itself failed: timeout or connection error."""


class UcasJsonError(UcasOperationalError):
    """The upstream answer was not the expected JSON object."""


class UcasServerError(UcasOperationalError):
    """The upstream answered with an HTTP error status or an error ``STATUS``."""


class UcasAuthError(UcasOperationalError):
    """Credentials are missing, malformed or rejected by the upstream.

    A malformed pair is caught locally (``BAD_CREDENTIALS``); a pair the
    upstream refuses is reported as ``AUTH_FAILED``.
    """


class UcasUnrecognizableCourse(UcasOperationalError):
    """The identifier is neither a course ID nor a timetable UUID (``BAD_IDENTIFIER``)."""


class UcasNotImplementedError(UcasError):
    """An unsupported upstream response; reported without ending the session."""
