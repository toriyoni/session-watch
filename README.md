# Session Watch

A terminal client for course sessions, QR codes, and periodic sign-in attempts.
It keeps the interactive session usable after rejected requests and provides an
optional polling mode with automatic reauthentication.

## Attribution and license

This project is a modified version of
[KINGFIOX/ucas-course-sign-in](https://github.com/KINGFIOX/ucas-course-sign-in),
based on upstream commit
[`9416eb7`](https://github.com/KINGFIOX/ucas-course-sign-in/commit/9416eb7ad4e2b3c3955c3757196c5b05775a229e).
The upstream Python implementation was itself based on
[lccipher/UCAS-Course-Sign-in](https://github.com/lccipher/UCAS-Course-Sign-in).
Credit for the original implementation and protocol work belongs to those
projects and their contributors.

Modifications in this version, dated **2026-09-29**, include:

- Recoverable sign-in failures that return to the selection prompt.
- Automatic polling in configurable ten-minute increments.
- Environment-based credentials and automatic reauthentication.
- Optional notifications that do not interrupt polling when delivery fails.
- A macOS launcher that prevents idle sleep while the client is running.
- Regression tests covering recovery, polling, date rollover, and response parsing.

The original Git history and [AGPL-3.0 license](LICENSE) are retained. This is an
independent derivative, not an official service or an endorsement by the
upstream authors or the institution operating the service.

## Requirements

- Python 3.10 or newer.
- [uv](https://docs.astral.sh/uv/).
- macOS or Linux for the terminal interface, which uses POSIX terminal APIs.

The macOS launcher additionally uses the system-provided `caffeinate` command.

## Quick start

```bash
git clone https://github.com/xiaoxiaojiujiu/session-watch.git
cd session-watch
uv sync --frozen --python 3.12
uv run --frozen tui
```

Enter your email address and password when prompted. Password input is hidden.
After login, the client displays today's courses and accepts the following
commands:

| Command | Action |
| --- | --- |
| `<number>` | Attempt sign-in for the selected course |
| `qr <number>` | Display a QR code for the selected course |
| `r` | Refresh today's course list |
| `auto` | Begin polling at the configured interval, defaulting to 10 minutes |
| `auto 20` | Begin polling every 20 minutes |
| `?` | Display help |
| `q` | Exit |

A rejected sign-in prints its reason and returns to `Select:`. Credentials stay
available for the current process, so a failed attempt does not require you to
start over. A successful attempt refreshes the course list.

In QR mode, a fresh code is printed every five seconds. Press `b` to return to
the course list. Displaying a QR code does not itself complete sign-in.

## Automatic mode

Start directly in automatic mode:

```bash
uv run --frozen tui --auto
```

Provide credentials once. The client immediately checks today's courses, then
waits ten minutes after each completed pass before checking again. To change
the interval:

```bash
uv run --frozen tui --auto --interval-minutes 20
```

Intervals must be positive multiples of ten minutes. Every course query and
sign-in request authenticates again with the captured credentials. Each pass
fetches the current date and course list; after midnight, it uses the new date.
Courses already marked as signed by the upstream service are skipped.

When automatic mode was entered through the `Select:` prompt, `Ctrl+C` returns
to that prompt. When started with `--auto`, `Ctrl+C` stops the program.

### macOS launcher

Double-click `start-auto.command` in Finder, or run:

```bash
./start-auto.command
```

The launcher loads `.env` when present, asks for any missing credentials, and
starts automatic mode. It uses `caffeinate -i` to prevent idle system sleep while
the process runs; the display can still sleep. Keep the terminal process alive
and the computer connected. Closing the lid, manually putting the computer to
sleep, shutting it down, or terminating the process prevents further polling.

### Saved configuration

Prompted credentials remain in memory and are not saved automatically. To reuse
credentials on later launches, create a local `.env` file:

```dotenv
UCAS_USERNAME=your-account@example.com
UCAS_PASSWORD='your-password'
UCAS_POLL_MINUTES=10
UCAS_RUN_ON_START=1
UCAS_FEISHU_WEBHOOK=
```

Use the actual email address accepted by the upstream service. Set file
permissions with `chmod 600 .env`. The file is ignored by Git. Use single quotes
around values containing characters such as `$` or `#`, and let uv load the file
instead of sourcing it as a shell script.

```bash
uv run --frozen --env-file .env tui --auto
```

`--interval-minutes` overrides `UCAS_POLL_MINUTES`. TUI automatic mode always
performs an initial check; only `server` honors `UCAS_RUN_ON_START`.

## Non-interactive server

For credentials supplied through environment variables or `.env`:

```bash
uv run --frozen --env-file .env server
```

The server runs the same polling logic without interactive prompts. Missing
credentials or an invalid interval cause a configuration error. `SIGTERM` and
`SIGINT` stop it gracefully.

| Variable | Default | Purpose |
| --- | --- | --- |
| `UCAS_USERNAME` | unset | Account email; required by `server`, prompted by TUI if absent |
| `UCAS_PASSWORD` | unset | Account password; required by `server`, prompted by TUI if absent |
| `UCAS_POLL_MINUTES` | `10` | Positive multiple of ten minutes |
| `UCAS_RUN_ON_START` | `1` | Run immediately when `server` starts |
| `UCAS_FEISHU_WEBHOOK` | unset | Optional notification webhook for `server` |
| `UCAS_FEISHU_SECRET` | unset | Optional webhook signature secret |

### Docker Compose

If `.env` does not already exist, copy `.env.example` and fill in the credentials.
Then run:

```bash
docker compose up -d --build
docker compose logs -f
```

Use `docker compose down` to stop. The container runs the polling server;
`UCAS_RUN_ON_START=0` delays the first pass by one interval.

### Optional notifications

Set `UCAS_FEISHU_WEBHOOK` to a Feishu custom-bot webhook to enable server
notifications. If the bot uses signature verification, also set
`UCAS_FEISHU_SECRET`. Leave the webhook empty for local logs only. TUI automatic
mode does not send notifications.

The server reports successful sign-ins and failures through the configured bot.
Routine waiting, no courses, and already-signed courses produce local logs only.
Delivery failures are printed locally without stopping polling. Failed
notifications are not queued for later delivery. Notification payloads contain
course names and outcomes, not account credentials.

## Recovery and timing

| Situation | Behavior |
| --- | --- |
| Sign-in is rejected | Show or log the reason; keep the session running |
| Network, timestamp, authentication, or schedule API error | Retry on the next automatic refresh |
| No courses, already signed, or outside the estimated window | Wait for the next refresh |
| A course has an unsupported time or an API failure | Record that failure and continue with other courses |
| Notification delivery fails | Print a local error and continue polling |
| A response explicitly confirms success | Report success; use the next course list to decide whether to skip it |

Automatic mode uses an estimated window from thirty minutes before class until
class ends, based on the calibrated upstream clock in `Asia/Shanghai`. The
upstream service may still reject an attempt within that window. Such a
rejection is retried on the next pass; polling does not override the upstream
service's acceptance rules. A window shorter than the polling interval can be
missed, and success is not guaranteed.

Expected API errors are handled at the command or polling-pass boundary.
Unsupported response shapes remain visible in logs. Unexpected programming
errors are not caught indiscriminately.

## Implementation notes

Requests are sent directly to the upstream endpoints on
`iclass.ucas.edu.cn:8181`. Compatibility with the inherited API, environment
variable names, and Python package name is preserved.

The shared client performs login, fetches the daily schedule, calibrates the
clock, and submits sign-in requests or builds QR payloads. The inherited
three-second timestamp buffer compensates for the upstream endpoints' clock
difference. Co-taught rows with the same course number and times are merged into
one displayed session.

```text
src/common/        Shared API client, data models, and typed errors
src/tui/           Interactive selection, QR codes, and automatic-mode entry
src/server/        Polling, per-pass sign-in, logging, and optional notifications
tests/             Offline regression tests with simulated upstream responses
start-auto.command macOS launcher
```

## Development and validation

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev ruff check src tests
```

The tests exercise rejection recovery, continued selection without another
credential prompt, automatic retries, polling intervals, window transitions,
date rollover, reauthentication, skipping signed courses, and notification
failures. They use mocks and HTTP transports without calling the live service.
Passing them does not establish live-service availability or successful sign-in
for a real account.
