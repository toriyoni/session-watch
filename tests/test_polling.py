import logging
from datetime import datetime
from unittest.mock import Mock

import httpx
import pytest

from common.api import (
    LOGIN_URL,
    SCHEDULE_URL,
    SIGN_URL,
    TIMESTAMP_URL,
    UCAS_TIMEZONE,
    Course,
    SignResult,
    UcasClient,
)
from common.error import UcasNetworkError, UcasNotImplementedError
from server.autosign import AutosignConfig, AutosignConfigError, parse_poll_minutes, run_once
from server.logging import NotifyHandler
from server.notify import NotifyError
from server.server import Server, main


def ms(day=29, hour=9, minute=0):
    return int(datetime(2026, 9, day, hour, minute, tzinfo=UCAS_TIMEZONE).timestamp() * 1000)


def course(identifier="1234567", **kwargs):
    fields = dict(
        id=identifier, course_name="测试课", class_begin_time="09:00", class_end_time="10:00"
    )
    fields.update(kwargs)
    return Course(**fields)


@pytest.mark.parametrize("value", ["0", "-10", "1", "15", "10.0", "abc"])
def test_invalid_poll_interval(value):
    with pytest.raises(AutosignConfigError):
        parse_poll_minutes(value)


@pytest.mark.parametrize("value", ["10", "20", "30", "60"])
def test_valid_poll_interval(value):
    assert parse_poll_minutes(value) == int(value)


def test_config_default_and_password_not_in_repr():
    config = AutosignConfig.from_env({"UCAS_USERNAME": "user", "UCAS_PASSWORD": "hidden-password"})
    assert config.poll_minutes == 10
    assert "hidden-password" not in repr(config)


def test_only_open_unsigned_courses_are_signed():
    client = Mock()
    client.server_now_ms.return_value = ms()
    client.query_courses.return_value = [
        course("1111111", sign_status="1"),
        course("2222222", class_begin_time="14:00", class_end_time="15:00"),
        course("3333333", class_begin_time="07:00", class_end_time="08:00"),
        course("4444444"),
    ]
    client.sign.return_value = SignResult(True, "ok")
    result = run_once(client, AutosignConfig("user", "password"))
    assert len(result.signed) == 1
    client.sign.assert_called_once_with("user", "password", "4444444")


def test_bad_course_or_rejection_does_not_block_remaining_courses():
    client = Mock()
    client.server_now_ms.return_value = ms()
    client.query_courses.return_value = [
        course("1111111", class_begin_time="invalid"),
        course("2222222"),
        course("3333333"),
    ]
    client.sign.side_effect = [SignResult(False, "not open", "101"), SignResult(True, "ok")]
    result = run_once(client, AutosignConfig("user", "password"))
    assert result.status == "failed"
    assert len(result.failed) == 2
    assert len(result.signed) == 1


@pytest.mark.parametrize(
    "failure", [UcasNetworkError("offline"), UcasNotImplementedError("new reply")]
)
def test_failed_fetch_retries_and_refetches_day_after_midnight(monkeypatch, failure):
    client = Mock()
    client.server_now_ms.side_effect = [failure, ms(), ms(day=30)]
    client.query_courses.return_value = []
    sleeps = Mock(side_effect=[None, None, KeyboardInterrupt])
    monkeypatch.setattr("server.server.time.sleep", sleeps)
    with pytest.raises(KeyboardInterrupt):
        Server(AutosignConfig("user", "password"), client).run_forever()
    assert [c.args[2] for c in client.query_courses.call_args_list] == ["20260929", "20260930"]
    assert [c.args for c in sleeps.call_args_list] == [(600,), (600,), (600,)]
    client.sign.assert_not_called()


def test_no_courses_keep_polling_and_run_on_start_false_waits(monkeypatch):
    client = Mock()
    client.server_now_ms.return_value = ms()
    client.query_courses.return_value = []
    sleeps = Mock(side_effect=[None, KeyboardInterrupt])
    monkeypatch.setattr("server.server.time.sleep", sleeps)
    with pytest.raises(KeyboardInterrupt):
        Server(AutosignConfig("user", "password", run_on_start=False), client).run_forever()
    client.query_courses.assert_called_once()
    client.sign.assert_not_called()


def test_notification_failure_does_not_end_signin_or_leak_webhook(capsys):
    notifier = Mock()
    notifier.send.side_effect = NotifyError("https://example.test/secret-token")
    handler = NotifyHandler(notifier)
    handler.emit(logging.LogRecord("server.autosign", logging.WARNING, "", 0, "signed", (), None))
    output = capsys.readouterr().err
    assert "will continue" in output
    assert "secret-token" not in output


def test_server_does_not_require_feishu(monkeypatch):
    monkeypatch.setenv("UCAS_USERNAME", "user")
    monkeypatch.setenv("UCAS_PASSWORD", "password")
    monkeypatch.delenv("UCAS_FEISHU_WEBHOOK", raising=False)
    monkeypatch.setattr("server.server.install_signal_handlers", lambda: None)
    client = Mock()
    client.server_now_ms.return_value = ms()
    client.query_courses.return_value = []
    monkeypatch.setattr("server.server.UcasClient", lambda: client)
    monkeypatch.setattr(
        "server.server.build_notifier", Mock(side_effect=AssertionError("no notifier"))
    )
    monkeypatch.setattr("server.server.time.sleep", Mock(side_effect=KeyboardInterrupt))
    assert main([]) == 0
    client.close.assert_called_once()


def test_mock_http_reauthenticates_and_retries_then_skips_signed_course(monkeypatch):
    """Exercise real API parsing, login and scheduler together with no network."""
    seen = []
    sign_attempts = 0
    signed = False

    def transport(request):
        nonlocal sign_attempts, signed
        endpoint = str(request.url).split("?")[0]
        seen.append(endpoint)
        if endpoint == TIMESTAMP_URL:
            return httpx.Response(200, json={"STATUS": "0", "timestamp": ms()})
        if endpoint == LOGIN_URL:
            return httpx.Response(
                200,
                json={"STATUS": "0", "result": {"sessionId": f"session-{len(seen)}", "id": "user"}},
            )
        if endpoint == SCHEDULE_URL:
            return httpx.Response(
                200,
                json={
                    "STATUS": "0",
                    "result": [
                        {
                            "id": "1234567",
                            "courseName": "测试课",
                            "courseNum": "course-1",
                            "classBeginTime": "09:00",
                            "classEndTime": "10:00",
                            "signStatus": "1" if signed else "0",
                        }
                    ],
                },
            )
        if endpoint == SIGN_URL:
            sign_attempts += 1
            if sign_attempts == 1:
                return httpx.Response(200, json={"ERRCODE": "101", "ERRMSG": "签到尚未开始"})
            signed = True
            return httpx.Response(200, json={"STATUS": "0", "result": {"stuSignStatus": "1"}})
        raise AssertionError(endpoint)

    client = UcasClient()
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(transport))
    sleeps = Mock(side_effect=[None, None, KeyboardInterrupt])
    monkeypatch.setattr("server.server.time.sleep", sleeps)
    try:
        with pytest.raises(KeyboardInterrupt):
            Server(AutosignConfig("user", "password"), client).run_forever()
    finally:
        client.close()
    assert sign_attempts == 2  # rejected once, succeeded once, then skipped
    assert seen.count(SCHEDULE_URL) == 3
    assert seen.count(LOGIN_URL) == 5  # three fresh queries + two fresh sign-ins
    assert [c.args for c in sleeps.call_args_list] == [(600,), (600,), (600,)]


def test_later_refresh_signs_when_window_opens(monkeypatch):
    client = Mock()
    client.server_now_ms.side_effect = [ms(hour=8, minute=20), ms(hour=8, minute=30)]
    client.query_courses.return_value = [course()]
    client.sign.return_value = SignResult(True, "ok")
    attempts_at_wait = []

    def wait(seconds):
        assert seconds == 600
        attempts_at_wait.append(client.sign.call_count)
        if len(attempts_at_wait) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("server.server.time.sleep", wait)
    with pytest.raises(KeyboardInterrupt):
        Server(AutosignConfig("user", "password"), client).run_forever()
    assert attempts_at_wait == [0, 1]


@pytest.mark.parametrize("body", [b"[]", b"null", b"<html>unavailable</html>"])
def test_malformed_notification_reply_does_not_stop_handler(body, capsys):
    from server.notify import FeishuNotifier

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    ) as http:
        notifier = FeishuNotifier("https://example.test/private-token", http_client=http)
        NotifyHandler(notifier).emit(
            logging.LogRecord("server.autosign", logging.WARNING, "", 0, "signed", (), None)
        )
    output = capsys.readouterr().err
    assert "will continue" in output
    assert "private-token" not in output
