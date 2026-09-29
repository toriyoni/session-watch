"""One automatic sign-in pass, shared by the TUI and polling server.

Each pass refreshes today's courses and reauthenticates using captured
credentials. Already-signed courses and courses outside the estimated window
are skipped. API errors are recorded and retried on the next polling pass;
one malformed course does not prevent attempts for other courses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from common.api import (
    Course,
    SignResult,
    UcasClient,
    format_date_from_ms,
    sign_window_state,
)
from common.error import UcasError

from .logging import get_logger

logger = get_logger(__name__)


class AutosignConfigError(Exception):
    """Raised when the runner is not configured correctly."""


def parse_poll_minutes(raw: str) -> int:
    """Polling is configured in positive ten-minute units."""
    try:
        minutes = int(raw)
    except ValueError:
        raise AutosignConfigError("poll interval must be 10, 20, 30, ... minutes") from None
    if minutes <= 0 or minutes % 10:
        raise AutosignConfigError("poll interval must be 10, 20, 30, ... minutes")
    return minutes


def _env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class AutosignConfig:
    username: str
    password: str = field(repr=False)
    run_on_start: bool = True
    poll_minutes: int = 10

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AutosignConfig":
        """Build the configuration from environment variables.

        Raises:
            AutosignConfigError: a required variable is missing.
        """
        env = env if env is not None else os.environ
        username = (env.get("UCAS_USERNAME") or "").strip()
        password = env.get("UCAS_PASSWORD") or ""

        if not username:
            raise AutosignConfigError("UCAS_USERNAME is required for automatic sign-in")
        if not password:
            raise AutosignConfigError("a password is required: set UCAS_PASSWORD")

        return cls(
            username=username,
            password=password,
            run_on_start=_env_bool(env, "UCAS_RUN_ON_START", True),
            poll_minutes=parse_poll_minutes(env.get("UCAS_POLL_MINUTES") or "10"),
        )


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class SignDetail:
    """Outcome of one sign-in attempt: success, or the error that stopped it."""

    course: Course
    result: SignResult | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.success

    @property
    def label(self) -> str:
        name = self.course.course_name or self.course.id
        return f"{name} [{self.course.id}]" if self.course.id else name

    @property
    def reason(self) -> str:
        if self.error:
            return self.error
        if self.result is not None:
            return self.result.message
        return "unknown error"


@dataclass
class AutosignResult:
    date: str
    status: str  # no-courses | idle | signed | failed | error
    signed: list[SignDetail] = field(default_factory=list)
    failed: list[SignDetail] = field(default_factory=list)
    error: str = ""

    @property
    def to_sign(self) -> int:
        return len(self.signed) + len(self.failed)


# --------------------------------------------------------------------------- #
# One run
# --------------------------------------------------------------------------- #


def run_once(client: UcasClient, config: AutosignConfig) -> AutosignResult:
    """Fetch today's courses and sign in for everything actionable.

    API errors become an AutosignResult. Programming errors still propagate.
    """
    # Use the calibrated UCAS clock to choose the date, including after midnight.
    try:
        now_ms = client.server_now_ms()
        date = format_date_from_ms(now_ms)
        courses = client.query_courses(config.username, config.password, date)
    except UcasError as exc:
        logger.fatal(
            "could not load the schedule for today: %s",
            exc.describe(),
            extra={"title": "UCAS auto sign-in: fetch failed"},
        )
        return AutosignResult(date="", status="error", error=exc.message)

    if not courses:
        logger.info("%s: no courses -- staying silent", date)
        return AutosignResult(date=date, status="no-courses")

    # 2. Pick the courses that still need a sign-in. UCAS itself is the source
    #    of truth: a course already signed in (`signStatus == "1"`) is skipped,
    #    and a course is only signed inside its sign-in window.
    pending: list[Course] = []
    result = AutosignResult(date=date, status="idle")
    for course in courses:
        if course.signed:
            continue
        try:
            window = sign_window_state(course, date, now_ms)
        except UcasError as exc:
            result.failed.append(SignDetail(course=course, error=exc.describe()))
            continue
        if window != "open":
            logger.info("%s: skip %s (window: %s)", date, course.course_name or course.id, window)
            continue
        pending.append(course)

    if not pending and not result.failed:
        logger.info("%s: %d course(s), nothing to sign in -- staying silent", date, len(courses))
        return AutosignResult(date=date, status="idle")

    # 3. Sign in. Each course is logged at INFO; the run as a whole is rolled up
    #    into one WARNING (success) / CRITICAL (failure) below, so the notify
    #    handler pushes at most one message per outcome instead of per course.
    for course in pending:
        outcome = _sign_one(client, config, course)
        if outcome.ok:
            result.signed.append(outcome)
            logger.info("%s: signed in for %s", date, course.course_name or course.id)
        else:
            result.failed.append(outcome)
            logger.info(
                "%s: sign-in failed for %s: %s",
                date,
                course.course_name or course.id,
                outcome.reason,
            )

    if result.signed:
        result.status = "signed"
        title, body = _success_message(date, result.signed)
        logger.warning(body, extra={"title": title})

    if result.failed:
        result.status = "failed"
        title, body = _failure_message(date, result.failed)
        logger.fatal(body, extra={"title": title})

    return result


def _sign_one(client: UcasClient, config: AutosignConfig, course: Course) -> SignDetail:
    """Convert an API failure for one course into a reportable result."""
    identifier = course.id or course.uuid
    try:
        sign_result = client.sign(config.username, config.password, identifier)
    except UcasError as exc:
        return SignDetail(course=course, error=exc.describe())
    return SignDetail(course=course, result=sign_result)


# --------------------------------------------------------------------------- #
# Message formatting
# --------------------------------------------------------------------------- #


def _format_course_line(detail: SignDetail) -> str:
    course = detail.course
    window = ""
    if course.class_begin_time and course.class_end_time:
        window = f" {_clock(course.class_begin_time)}~{_clock(course.class_end_time)}"
    return f"- {detail.label}{window}: {detail.reason}"


def _clock(value: str) -> str:
    tail = value.strip().split()[-1] if value.strip() else ""
    return tail[:5] if tail else "--"


def _success_message(date: str, signed: list[SignDetail]) -> tuple[str, str]:
    lines = [f"Date: {_pretty_date(date)}", "", *(_format_course_line(item) for item in signed)]
    return f"UCAS auto sign-in succeeded ({len(signed)})", "\n".join(lines)


def _failure_message(date: str, failed: list[SignDetail]) -> tuple[str, str]:
    lines = [
        f"Date: {_pretty_date(date)}",
        "",
        "The following courses could not be signed in; retrying on the next refresh:",
        "",
        *(_format_course_line(item) for item in failed),
    ]
    return f"UCAS auto sign-in failed ({len(failed)})", "\n".join(lines)


def _pretty_date(compact: str) -> str:
    if len(compact) == 8 and compact.isdigit():
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    return compact
