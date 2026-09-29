from datetime import datetime
from unittest.mock import Mock

import pytest

from common.api import UCAS_TIMEZONE, Course, LoginResult, SignResult, parse_sign_response
from common.error import UcasJsonError, UcasNetworkError, UcasNotImplementedError
from tui import tui


@pytest.mark.parametrize(
    "payload",
    [
        {"ERRCODE": "101", "ERRMSG": "二维码已失效！"},
        {"STATUS": "1", "ERRMSG": "签到尚未开始"},
        {"STATUS": 0, "ERRCODE": 101, "result": {"stuSignStatus": "1"}},
        {"STATUS": "0", "result": {"stuSignStatus": "0"}},
    ],
)
def test_rejected_signin_is_not_success_or_a_crash(payload):
    result = parse_sign_response(payload)
    assert not result.success
    assert result.message


@pytest.mark.parametrize("status", ["0", 0])
def test_explicit_success_supports_string_and_integer_status(status):
    result = parse_sign_response(
        {"STATUS": status, "result": {"stuSignStatus": 1, "stuSignId": 123}}
    )
    assert result.success
    assert result.stu_sign_id == "123"


def test_malformed_reply_is_recoverable():
    with pytest.raises(UcasJsonError):
        parse_sign_response({"unexpected": []})


def make_client():
    client = Mock()
    client.login.return_value = LoginResult("session", "user")
    client.server_now_ms.return_value = int(
        datetime(2026, 9, 29, 9, tzinfo=UCAS_TIMEZONE).timestamp() * 1000
    )
    client.query_courses.return_value = [
        Course(id="1234567", course_name="测试课", class_begin_time="09:00", class_end_time="10:00")
    ]
    return client


@pytest.mark.parametrize(
    "failure",
    [
        SignResult(False, "签到尚未开始", "101"),
        UcasNetworkError("temporarily offline"),
        UcasNotImplementedError("unrecognized sign response"),
    ],
)
def test_failed_selection_returns_to_menu_without_reprompting_password(
    monkeypatch, capsys, failure
):
    monkeypatch.delenv("UCAS_USERNAME", raising=False)
    monkeypatch.delenv("UCAS_PASSWORD", raising=False)
    inputs = iter(["student@example.test", "1", "1", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    password = Mock(return_value="test-password")
    monkeypatch.setattr(tui.getpass, "getpass", password)
    client = make_client()
    client.sign.side_effect = [failure, SignResult(True, "Sign-in successful")]
    assert tui.main([], client=client) == 0
    assert client.sign.call_count == 2
    assert client.login.call_count == 1  # one interactive authentication, no login prompt restart
    assert password.call_count == 1
    assert client.query_courses.call_count == 2  # only reload after success
    assert "Sign-in successful" in capsys.readouterr().out
    client.close.assert_called_once()


def test_clock_failure_can_be_reloaded_from_selection(monkeypatch):
    monkeypatch.setenv("UCAS_USERNAME", "student@example.test")
    monkeypatch.setenv("UCAS_PASSWORD", "test-password")
    inputs = iter(["r", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    client = make_client()
    now = client.server_now_ms.return_value
    client.server_now_ms.side_effect = [UcasNetworkError("offline"), now, now]
    assert tui.main([], client=client) == 0
    client.login.assert_called_once()
    client.query_courses.assert_called_once()


def test_auto_command_reuses_session_and_ctrl_c_returns_to_menu(monkeypatch, capsys):
    monkeypatch.setenv("UCAS_USERNAME", "student@example.test")
    monkeypatch.setenv("UCAS_PASSWORD", "test-password")
    inputs = iter(["auto 20", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    sleeps = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr("server.server.time.sleep", sleeps)
    client = make_client()
    client.sign.return_value = SignResult(True, "ok")
    assert tui.main([], client=client) == 0
    sleeps.assert_called_once_with(1200)
    client.login.assert_called_once()
    assert "Automatic sign-in stopped" in capsys.readouterr().out


def test_auto_start_uses_environment_and_retries_initial_network_failure(monkeypatch):
    monkeypatch.setenv("UCAS_USERNAME", "student@example.test")
    monkeypatch.setenv("UCAS_PASSWORD", "test-password")
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("must not prompt")))
    client = make_client()
    now = client.server_now_ms.return_value
    client.server_now_ms.side_effect = [UcasNetworkError("offline"), now]
    client.sign.return_value = SignResult(True, "ok")
    sleeps = Mock(side_effect=[None, KeyboardInterrupt])
    monkeypatch.setattr("server.server.time.sleep", sleeps)
    assert tui.main(["--auto"], client=client) == 0
    assert [call.args for call in sleeps.call_args_list] == [(600,), (600,)]
    client.sign.assert_called_once_with("student@example.test", "test-password", "1234567")
    client.close.assert_called_once()
