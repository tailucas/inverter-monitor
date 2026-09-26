#!/usr/bin/env python
"""Unit tests for Telegram bot helpers (data shaping, notification dispatch)."""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from tailucas_pylib import APP_NAME
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

import app.bot as bot_module
from app.bot import TelegramBot, _is_permanent_recipient_error, _to_df
from app.metrics import PrometheusMetricDTO


def test_to_df_label_columns() -> None:
    """Cell series are keyed by pack and cell labels."""
    results = [
        PrometheusMetricDTO(
            ts_ms=1000.0,
            value=3.30,
            metric_name="cell_voltage_v",
            labels={"bms_addr": "BMS01", "cell": "01"},
        ),
        PrometheusMetricDTO(
            ts_ms=1000.0,
            value=3.28,
            metric_name="cell_voltage_v",
            labels={"bms_addr": "BMS01", "cell": "02"},
        ),
        PrometheusMetricDTO(
            ts_ms=2000.0,
            value=3.31,
            metric_name="cell_voltage_v",
            labels={"bms_addr": "BMS01", "cell": "01"},
        ),
    ]
    df = _to_df(results, use_labels=True)
    assert set(df.columns) == {"_time", "BMS01 c01", "BMS01 c02"}
    assert df["BMS01 c01"].tolist() == [3.30, 3.31]


def test_to_df_metric_columns() -> None:
    """Unlabeled series keep the friendly metric name as the column."""
    results = [
        PrometheusMetricDTO(ts_ms=1000.0, value=2000.0, metric_name="total_power_w"),
        PrometheusMetricDTO(ts_ms=2000.0, value=2100.0, metric_name="total_power_w"),
    ]
    df = _to_df(results)
    assert list(df.columns) == ["_time", "total_power_w"]
    assert df["total_power_w"].tolist() == [2000.0, 2100.0]


def test_to_df_empty() -> None:
    """No results yields an empty DataFrame."""
    assert _to_df([]).empty


class FakeBot:
    """Minimal Bot stand-in that raises a configured error."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[int] = []

    async def send_message(
        self, chat_id: int, text: str, parse_mode: Any = None
    ) -> None:
        self.calls.append(chat_id)
        if self.error is not None:
            raise self.error


class SelectiveBot(FakeBot):
    """Bot stand-in that fails for selected chat IDs only."""

    def __init__(self, failing_chat_ids: set[int], error: Exception) -> None:
        super().__init__(None)
        self.failing_chat_ids = failing_chat_ids
        self.failure = error

    async def send_message(
        self, chat_id: int, text: str, parse_mode: Any = None
    ) -> None:
        self.calls.append(chat_id)
        if chat_id in self.failing_chat_ids:
            raise self.failure


class FakeApplication:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot


class FakeCreds:
    def get_creds(self, path: str) -> str:
        return "123:FAKE"


class FakeConfig:
    """Config stand-in: real recipients, code fallbacks for everything else."""

    def __init__(self, chat_room_id: int | str = 0) -> None:
        self.chat_room_id = chat_room_id

    def get(self, section: str, option: str, **kwargs: Any) -> str:
        if (section, option) == ("telegram", "enabled_users_csv"):
            return "111,222"
        if (section, option) == ("telegram", "chat_room_id"):
            return str(self.chat_room_id) if self.chat_room_id else ""
        return str(kwargs.get("fallback", ""))

    def getint(
        self, section: str, option: str, fallback: int = 0, **kwargs: Any
    ) -> int:
        return fallback

    def getfloat(
        self, section: str, option: str, fallback: float = 0.0, **kwargs: Any
    ) -> float:
        return fallback

    def getboolean(
        self, section: str, option: str, fallback: bool = False, **kwargs: Any
    ) -> bool:
        return fallback


def _fake_validate_update(
    user_id: int,
    chat_id: int,
    chat_type: str = "private",
    chat_title: str | None = None,
) -> Any:
    """Minimal Update stand-in carrying the attributes validate() reads."""
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, is_bot=False, language_code="en"),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title=chat_title),
        effective_message=None,
    )


def test_validate_logs_chat_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Authorized commands log the chat id/type/title and user id."""
    monkeypatch.setattr(bot_module, "app_config", FakeConfig())
    update = _fake_validate_update(
        user_id=111,
        chat_id=-100123,
        chat_type="supergroup",
        chat_title="Inverters",
    )
    with caplog.at_level(logging.INFO, logger=APP_NAME):
        user = asyncio.run(bot_module.validate("status", update))
    assert user is update.effective_user
    records = [
        record for record in caplog.records if record.getMessage() == "Telegram command"
    ]
    assert len(records) == 1
    record: Any = records[0]
    assert record.command == "status"
    assert record.chat_id == -100123
    assert record.chat_type == "supergroup"
    assert record.chat_title == "Inverters"
    assert record.user_id == 111


def test_validate_logs_chat_context_for_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unauthorized commands still log the chat context for discovery."""
    monkeypatch.setattr(bot_module, "app_config", FakeConfig())
    update = _fake_validate_update(user_id=999, chat_id=-100999, chat_type="group")
    with caplog.at_level(logging.INFO, logger=APP_NAME):
        assert asyncio.run(bot_module.validate("status", update)) is None
    records = [
        record
        for record in caplog.records
        if record.getMessage() == "Ignoring user not in allowlist"
    ]
    assert len(records) == 1
    record: Any = records[0]
    assert record.chat_id == -100999
    assert record.chat_type == "group"
    assert record.chat_title is None


def test_is_permanent_recipient_error() -> None:
    """Only unrecoverable recipient failures are treated as permanent."""
    assert _is_permanent_recipient_error(
        Forbidden("bot can't initiate conversation with a user")
    )
    assert _is_permanent_recipient_error(BadRequest("Chat not found"))
    assert not _is_permanent_recipient_error(BadRequest("Message is too long"))
    assert not _is_permanent_recipient_error(TimedOut("Timed out"))


def test_send_notification_mutes_permanent_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Permanent recipient failures warn once and are muted for the process."""
    monkeypatch.setattr(bot_module, "app_config", FakeConfig())
    bot = TelegramBot(creds_obj=FakeCreds())
    fake_bot = FakeBot(Forbidden("bot can't initiate conversation with a user"))
    setattr(bot, "_application", FakeApplication(fake_bot))  # noqa: B010
    payload = {
        "load_alert": {
            "kind": "load_warning",
            "load_w": 7100.0,
            "threshold_w": 7000.0,
        }
    }

    with caplog.at_level(logging.WARNING, logger=APP_NAME):
        asyncio.run(bot._send_notification(payload))
    assert fake_bot.calls == [111, 222]
    assert bot._unreachable_users == {111, 222}
    muting = [
        record
        for record in caplog.records
        if record.getMessage()
        == "Telegram recipient cannot receive notifications; muting"
    ]
    assert len(muting) == 2
    record: Any = muting[0]
    assert record.error_type == "telegram.error.Forbidden"
    assert record.chat_id == "111"

    caplog.clear()
    asyncio.run(bot._send_notification(payload))
    assert fake_bot.calls == [111, 222]
    assert not [
        record
        for record in caplog.records
        if record.getMessage()
        == "Telegram recipient cannot receive notifications; muting"
    ]


def test_send_notification_continues_after_permanent_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One unreachable recipient must not block delivery to the others."""
    monkeypatch.setattr(bot_module, "app_config", FakeConfig())
    bot = TelegramBot(creds_obj=FakeCreds())
    fake_bot = SelectiveBot(
        {111}, Forbidden("bot can't initiate conversation with a user")
    )
    setattr(bot, "_application", FakeApplication(fake_bot))  # noqa: B010
    payload = {
        "load_alert": {
            "kind": "load_recovery",
            "load_w": 1121.0,
            "threshold_w": 7000.0,
            "cooldown_secs": 600.0,
        }
    }

    with caplog.at_level(logging.INFO, logger=APP_NAME):
        asyncio.run(bot._send_notification(payload))
    # both recipients were attempted; only the reachable one succeeded
    assert fake_bot.calls == [111, 222]
    assert bot._unreachable_users == {111}
    dispatched = [
        record
        for record in caplog.records
        if record.getMessage() == "Telegram notification dispatched"
    ]
    assert len(dispatched) == 1
    summary: Any = dispatched[0]
    assert summary.recipient_count == 1
    assert summary.unreachable_count == 0

    # the next notification skips the muted recipient but still delivers
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=APP_NAME):
        asyncio.run(bot._send_notification(payload))
    assert fake_bot.calls == [111, 222, 222]
    dispatched = [
        record
        for record in caplog.records
        if record.getMessage() == "Telegram notification dispatched"
    ]
    assert len(dispatched) == 1
    summary = dispatched[0]
    assert summary.recipient_count == 1
    assert summary.unreachable_count == 1


class RateLimitedBot(FakeBot):
    """Bot stand-in that raises RetryAfter once for a selected chat ID."""

    def __init__(self, limited_chat_id: int, retry_after: int) -> None:
        super().__init__(None)
        self.limited_chat_id = limited_chat_id
        self.retry_after = retry_after
        self.remaining_limits = 1

    async def send_message(
        self, chat_id: int, text: str, parse_mode: Any = None
    ) -> None:
        self.calls.append(chat_id)
        if chat_id == self.limited_chat_id and self.remaining_limits > 0:
            self.remaining_limits -= 1
            raise RetryAfter(retry_after=self.retry_after)


def test_notification_chat_ids_prefers_chat_room(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A configured group room is the sole notification destination."""
    monkeypatch.setattr(bot_module, "app_config", FakeConfig(chat_room_id=-100123))
    assert bot_module._notification_chat_ids() == ["-100123"]
    monkeypatch.setattr(bot_module, "app_config", FakeConfig())
    assert bot_module._notification_chat_ids() == ["111", "222"]
    # a malformed room id warns and falls back to the per-user list
    monkeypatch.setattr(
        bot_module, "app_config", FakeConfig(chat_room_id="not-a-number")
    )
    with caplog.at_level(logging.WARNING, logger=APP_NAME):
        assert bot_module._notification_chat_ids() == ["111", "222"]
    assert [
        record
        for record in caplog.records
        if record.getMessage() == "Ignoring non-numeric Telegram chat room id"
    ]


def test_send_notification_uses_chat_room_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bot-initiated messages go to the group room when configured."""
    monkeypatch.setattr(bot_module, "app_config", FakeConfig(chat_room_id=-100123))
    bot = TelegramBot(creds_obj=FakeCreds())
    fake_bot = FakeBot()
    setattr(bot, "_application", FakeApplication(fake_bot))  # noqa: B010
    payload = {
        "load_alert": {
            "kind": "load_warning",
            "load_w": 7100.0,
            "threshold_w": 7000.0,
        }
    }

    asyncio.run(bot._send_notification(payload))
    assert fake_bot.calls == [-100123]


def test_send_notification_retries_after_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rate-limited send is deferred (bounded) and retried once."""
    monkeypatch.setattr(bot_module, "app_config", FakeConfig())
    monkeypatch.setattr(bot_module, "MAX_TELEGRAM_RETRY_WAIT_SECONDS", 0)
    bot = TelegramBot(creds_obj=FakeCreds())
    fake_bot = RateLimitedBot(limited_chat_id=222, retry_after=1)
    setattr(bot, "_application", FakeApplication(fake_bot))  # noqa: B010
    payload = {
        "load_alert": {
            "kind": "load_warning",
            "load_w": 7100.0,
            "threshold_w": 7000.0,
        }
    }

    with caplog.at_level(logging.INFO, logger=APP_NAME):
        asyncio.run(bot._send_notification(payload))
    # the 222 recipient is attempted twice: once limited, once retried
    assert fake_bot.calls == [111, 222, 222]
    rate_limited = [
        record
        for record in caplog.records
        if record.getMessage() == "Telegram rate limit; deferring notification"
    ]
    assert len(rate_limited) == 1
    record: Any = rate_limited[0]
    assert float(record.retry_after_seconds) == 1.0
    assert record.wait_seconds == 0
    dispatched = [
        record
        for record in caplog.records
        if record.getMessage() == "Telegram notification dispatched"
    ]
    summary: Any = dispatched[0]
    assert summary.recipient_count == 2
