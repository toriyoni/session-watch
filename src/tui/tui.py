"""Interactive command-line interface.

There is no full-screen UI. The program asks a short sequence of questions in
the terminal -- in the spirit of FreeBSD's ``adduser`` -- and then runs a small
command loop over today's courses:

* :func:`_login` prompts for the mail and password until the credentials
  are accepted (the password is read with :func:`getpass.getpass`, so it is
  never echoed).
* :func:`_course_loop` lists today's courses and reads commands such as a
  course number, ``qr <number>``, ``r``, ``?`` or ``q``.

The date is never asked for: it is taken from the calibrated UCAS server clock,
so it is always "today" as the upstream understands it.

API errors are reported at the command boundary without discarding credentials.
Automatic mode retries on the next polling pass and returns to the selection
menu when interrupted.
"""

from __future__ import annotations

import argparse
import getpass
import os
import select
import sys
import termios
import time
import tty
import unicodedata
from dataclasses import dataclass, field

import qrcode

from common import __version__
from common.api import (
    Course,
    SignResult,
    UcasClient,
    build_scan_url,
    format_date_from_ms,
    format_time_range,
    normalize_course_sched_id,
    normalize_uuid,
    sign_window_state,
)
from common.error import (
    UcasAuthError,
    UcasError,
    UcasJsonError,
    UcasNetworkError,
    UcasServerError,
    UcasUnrecognizableCourse,
)
from server.autosign import AutosignConfig, AutosignConfigError, parse_poll_minutes
from server.logging import configure_logging
from server.server import Server

WINDOW_TEXT = {
    "open": "Open",
    "before": "Not open",
    "after": "Closed",
}

HELP_LINES = (
    "  <number>     sign in for the course with that number",
    "  qr <number>  show a scannable sign-in QR code for that course",
    "  r            reload today's courses",
    "  auto [10]    auto sign-in every 10, 20, 30, ... minutes; Ctrl+C to return",
    "  ?            show this help",
    "  q            quit",
)


class _Quit(Exception):
    """Raised internally when the user asks to stop."""


@dataclass
class Session:
    """Credentials captured at login and reused for the whole session."""

    username: str
    password: str = field(repr=False)


# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #


def _char_width(char: str) -> int:
    """Display width of a single character (CJK glyphs are two columns wide)."""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    padding = max(0, width - _width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def _ask(label: str, default: str = "") -> str:
    """Prompt for a line of input, honoring an optional default."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{label}{suffix}: ").strip()
    except EOFError:
        raise _Quit from None
    return answer or default


def _ask_password(label: str = "Password") -> str:
    try:
        return getpass.getpass(f"{label}: ")
    except EOFError:
        raise _Quit from None


def _pretty_date(compact: str) -> str:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def _error_hint(exc: UcasError) -> str:
    """A short, type-aware line explaining an error to an interactive user."""
    if isinstance(exc, UcasAuthError):
        return f"Credentials rejected: {exc.message}"
    if isinstance(exc, UcasNetworkError):
        return f"Network problem: {exc.message}"
    if isinstance(exc, UcasServerError):
        return f"UCAS server error: {exc.message}"
    if isinstance(exc, UcasJsonError):
        return f"Unexpected reply from UCAS: {exc.message}"
    if isinstance(exc, UcasUnrecognizableCourse):
        return f"Unrecognizable course: {exc.message}"
    return exc.describe()


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


def _read_session(*, from_env: bool = True) -> Session:
    """Use configured credentials or ask once, without writing them to disk."""
    username = (os.environ.get("UCAS_USERNAME") or "").strip() if from_env else ""
    password = (os.environ.get("UCAS_PASSWORD") or "") if from_env else ""
    while not username:
        username = _ask("Mail")
        if not username:
            print("Mail is required.")
    while not password:
        password = _ask_password()
        if not password:
            print("Password is required.")
    return Session(username, password)


def _login(client: UcasClient) -> Session:
    """Prompt for the mail and password until the upstream accepts them.

    Environment variables can supply credentials. Rejected credentials go
    back to the prompts; transient errors retry the same ones.
    """
    session = _read_session()
    while True:
        while True:
            print("Signing in... ", end="", flush=True)
            try:
                client.login(session.username, session.password)
            except UcasAuthError as exc:
                print("failed.")
                print(f"  {_error_hint(exc)}")
                print()
                session = _read_session(from_env=False)
                break  # ask for the mail and password again
            except UcasError as exc:
                print("failed.")
                print(f"  {_error_hint(exc)}")
                print("  Retrying in a moment; press Ctrl+C to give up.")
                print()
                time.sleep(2.0)
                continue
            print("done.")
            return session


# --------------------------------------------------------------------------- #
# Course list
# --------------------------------------------------------------------------- #


def _today(client: UcasClient) -> str:
    """Today's date (``yyyyMMdd``) from the calibrated server clock."""
    return format_date_from_ms(client.server_now_ms())


def _print_courses(client: UcasClient, courses: list[Course], date: str) -> None:
    if not courses:
        print(f"  No courses found for {_pretty_date(date)}.")
        return

    now = client.server_now_ms()
    rows = [("#", "Course", "Teacher", "Time", "Status", "Sign-in window")]
    for index, course in enumerate(courses, start=1):
        state = sign_window_state(course, date, now)
        rows.append(
            (
                str(index),
                course.course_name or "--",
                course.teacher_name or "--",
                format_time_range(course.class_begin_time, course.class_end_time),
                course.status_text,
                WINDOW_TEXT.get(state, "--"),
            )
        )

    widths = [max(_width(row[column]) for row in rows) for column in range(len(rows[0]))]
    print()
    for row_index, row in enumerate(rows):
        cells = [
            _pad(cell, widths[column], align="right" if column == 0 else "left")
            for column, cell in enumerate(row)
        ]
        print("  " + "  ".join(cells).rstrip())
        if row_index == 0:
            print("  " + "  ".join("-" * width for width in widths))
    print()


def _load_courses(client: UcasClient, session: Session, date: str) -> list[Course]:
    print(f"Fetching courses for {_pretty_date(date)}... ", end="", flush=True)
    try:
        courses = client.query_courses(session.username, session.password, date)
    except UcasError as exc:
        print("failed.")
        print(f"  {_error_hint(exc)}")
        return []
    print("done.")
    _print_courses(client, courses, date)
    return courses


# --------------------------------------------------------------------------- #
# Sign-in
# --------------------------------------------------------------------------- #


def _report_sign(result: SignResult) -> bool:
    """Report the outcome and leave failed attempts in the selection loop."""
    if not result.success:
        print("not signed.")
        print(f"  ✗ {result.message}")
        print("  You can select another course, or type auto to retry automatically.")
        return False
    print("done.")
    detail = f" (sign-in record {result.stu_sign_id})" if result.stu_sign_id else ""
    print(f"  ✓ {result.message}{detail}")
    return True


def _sign(client: UcasClient, session: Session, identifier: str, label: str) -> bool:
    if normalize_course_sched_id(identifier) is None and normalize_uuid(identifier) is None:
        print("  Invalid ID: use a 7-digit course ID or a 32-char hex UUID.")
        return False

    print(f"Signing in for {label}... ", end="", flush=True)
    try:
        result = client.sign(session.username, session.password, identifier)
    except UcasError as exc:
        print("failed.")
        print(f"  ✗ {_error_hint(exc)}")
        return False
    return _report_sign(result)


def _print_help() -> None:
    print("Enter a course number to sign in, or one of:")
    for line in HELP_LINES:
        print(line)


# --------------------------------------------------------------------------- #
# QR code
# --------------------------------------------------------------------------- #

_QR_STYLE = "\x1b[30;47m"  # black foreground on white background
_QR_RESET = "\x1b[0m"
_QR_HINT = "Scan with the UCAS mobile app. Press 'b' to go back."

# Keep the on-screen code fresh: the sign-in URL embeds a timestamp that the
# upstream validates, so a stale code is rejected. Matches the web version.
QR_REFRESH_SECONDS = 5.0


def _qr_matrix(payload: str) -> list[list[bool]]:
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    code.add_data(payload)
    code.make(fit=True)
    return code.get_matrix()


def _qr_lines(payload: str) -> list[str]:
    """Return the QR code as lines of half-block characters."""
    matrix = _qr_matrix(payload)
    width = len(matrix[0]) if matrix else 0
    lines = []
    for row_index in range(0, len(matrix), 2):
        top = matrix[row_index]
        bottom = matrix[row_index + 1] if row_index + 1 < len(matrix) else [False] * width
        cells = []
        for column in range(width):
            upper, lower = top[column], bottom[column]
            if upper and lower:
                cells.append("█")
            elif upper:
                cells.append("▀")
            elif lower:
                cells.append("▄")
            else:
                cells.append(" ")
        lines.append(_QR_STYLE + "".join(cells) + _QR_RESET)
    return lines


def _read_key(timeout: float) -> str | None:
    """Read a single keypress, or return ``None`` when ``timeout`` elapses.

    Without a terminal (output piped/redirected) there is no keypress to read,
    so the timeout is simply slept through.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        time.sleep(timeout)
        return None
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
        return os.read(fd, 1).decode(errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _wait_for_back(seconds: float) -> bool:
    """Wait up to ``seconds``, returning ``True`` as soon as ``b`` is pressed.

    Any other key is ignored: the QR code refreshes on its own 5-second
    schedule, there is no way to refresh it earlier.
    """
    deadline = time.monotonic() + seconds
    remaining = seconds
    while remaining > 0:
        key = _read_key(remaining)
        if key is None:
            return False
        if key.lower() == "b":
            return True
        remaining = deadline - time.monotonic()
    return False


def _print_qr(payload: str) -> None:
    """Render ``payload`` as a QR code using half-block characters.

    Colours are forced (black on white) so the code scans regardless of the
    terminal's light/dark theme.
    """
    print()
    for line in _qr_lines(payload):
        print("  " + line)
    print()


def _show_qr(client: UcasClient, course: Course) -> None:
    """Display a live sign-in QR code for ``course``.

    The QR encodes the upstream sign-in URL (without a user id, exactly like the
    web version). The URL carries a timestamp, so it must stay fresh: a new code
    is printed every :data:`QR_REFRESH_SECONDS` seconds until the user goes back.
    """
    _show_qr_refreshing(client, course, course.course_name or course.id)


def _show_qr_refreshing(client: UcasClient, course: Course, label: str) -> None:
    """Print a fresh QR code every few seconds; press 'b' to go back.

    The output simply scrolls -- the newest code is always the last one -- so it
    works in any terminal without cursor-positioning support. The hint line is
    repeated with every code so it scrolls along with it. Codes are refreshed on
    a fixed schedule only, never on a keypress.
    """
    interval = QR_REFRESH_SECONDS
    print(f"Sign-in QR for '{label}': a new code is printed every {interval:g}s.")
    while True:
        timestamp = client.sign_timestamp()
        payload = build_scan_url(course.id, timestamp)
        _print_qr(payload)
        print(f"  {payload}")
        print(f"  {_QR_HINT}")
        if _wait_for_back(interval):
            return


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


def _run_auto(client: UcasClient, session: Session, minutes: int) -> None:
    print(f"Automatic sign-in enabled: refresh every {minutes} minutes.")
    print("Keep this terminal open and the computer awake. Ctrl+C stops automatic mode.")
    configure_logging()
    config = AutosignConfig(session.username, session.password, poll_minutes=minutes)
    try:
        Server(config, client).run_forever()
    except KeyboardInterrupt:
        print("\nAutomatic sign-in stopped.")


def _course_loop(client: UcasClient, session: Session, minutes: int = 10) -> None:
    courses: list[Course] = []
    reload_courses = True
    _print_help()
    while True:
        try:
            if reload_courses:
                reload_courses = False
                date = _today(client)
                courses = _load_courses(client, session, date)

            print()
            command = _ask("Select")
            if not command:
                continue
            lowered = command.lower()
            if lowered in ("q", "quit", "exit"):
                raise _Quit
            if lowered in ("?", "help", "h"):
                _print_help()
                continue
            if lowered in ("r", "reload", "refresh"):
                reload_courses = True
                continue
            if lowered == "auto" or lowered.startswith("auto "):
                parts = lowered.split()
                try:
                    interval = parse_poll_minutes(parts[1]) if len(parts) == 2 else minutes
                    if len(parts) > 2:
                        raise AutosignConfigError("usage: auto [10, 20, 30, ...]")
                except AutosignConfigError as exc:
                    print(f"  {exc}")
                    continue
                _run_auto(client, session, interval)
                reload_courses = True
                continue

            is_qr = lowered == "qr" or lowered.startswith("qr ")
            argument = command[2:].strip() if is_qr else command
            if is_qr and not argument:
                argument = _ask("Course number")
            if not argument.isdigit():
                print(f"  Unrecognized input: {command!r}. Type ? for help.")
                continue
            index = int(argument)
            if not 1 <= index <= len(courses):
                if courses:
                    print(f"  Enter a number between 1 and {len(courses)}.")
                else:
                    print("  No courses loaded; use 'r' to reload.")
                continue
            course = courses[index - 1]
            if is_qr:
                _show_qr(client, course)
                continue
            if course.signed:
                print("  This course is already signed in.")
                continue
            label = course.course_name or course.id or course.uuid
            if _sign(client, session, course.id or course.uuid, f"'{label}'"):
                reload_courses = True
        except UcasError as exc:
            # Recover from a rejected sign-in, unavailable clock, or malformed
            # upstream response without losing the captured credentials.
            print(f"  {_error_hint(exc)}")
            print(
                "  Credentials kept for this session. Select another course, r to reload, or auto."
            )


def main(argv: list[str] | None = None, *, client: UcasClient | None = None) -> int:
    """Program entry point."""
    parser = argparse.ArgumentParser(
        description="UCAS course sign-in",
        epilog="Credentials: UCAS_USERNAME/UCAS_PASSWORD, or enter them once when prompted. "
        "In the course menu, type auto to poll; Ctrl+C returns to the menu.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"ucas-course-sign-in {__version__}"
    )
    parser.add_argument("--auto", action="store_true", help="refresh and sign in automatically")
    parser.add_argument("--interval-minutes", default=None, help="10, 20, 30, ... minutes")
    args = parser.parse_args(argv)
    try:
        minutes = parse_poll_minutes(
            args.interval_minutes or os.environ.get("UCAS_POLL_MINUTES") or "10"
        )
    except AutosignConfigError as exc:
        parser.error(str(exc))

    print("UCAS Course Sign-in")
    print("===================")

    active_client = client or UcasClient()
    try:
        if args.auto:
            # Authentication happens inside each pass so an outage at startup
            # does not prevent the next scheduled attempt.
            session = _read_session()
            _run_auto(active_client, session, minutes)
        else:
            session = _login(active_client)
            _course_loop(active_client, session, minutes)
    except _Quit:
        print("\nBye.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except UcasError as exc:
        print(f"\nError: {_error_hint(exc)}")
        return 1
    finally:
        active_client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
