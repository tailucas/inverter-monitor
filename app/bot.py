#!/usr/bin/env python
"""Telegram bot thread and command handlers for inverter-monitor.

Follows conventions from .clinerules/telegram.md:
handler signatures, standard skeleton, parse modes, emoji, error handler,
and terminator shutdown.

Pure helper functions and data structures are in `app.telegram_bot`.
"""

import asyncio
import threading
import time
from asyncio import AbstractEventLoop
from collections.abc import Callable
from typing import Any

import emoji
import pandas as pd
import zmq
from tailucas_pylib import app_config, log, threads
from tailucas_pylib.app import AppThread
from tailucas_pylib.zmq import Closable
from telegram import Update
from telegram import User as TelegramUser
from telegram.constants import ChatAction, ParseMode
from telegram.error import TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.metrics import (
    BATTERY_QUERIES,
    POWER_QUERIES,
    fetch_metrics,
)
from app.telegram_bot import (
    DEFAULT_HISTORY_HOURS,
    URL_WORKER_TELEGRAM,
    StatusBuffer,
    StatusSnapshot,
    _get_telegram_token,
    build_history_caption,
    format_status_message,
    render_battery_chart,
    render_power_chart,
)

# -- terminator ----------------------------------------------------------------


def terminator(
    loop: AbstractEventLoop,
) -> None:
    """Wait for shutdown signal, then stop the asyncio loop.

    PTB's __run() finally block handles updater/application shutdown;
    this just interrupts run_forever so that the finally block can run.
    """
    log.debug(
        "asyncio loop terminator is ready.",
        extra={"app_thread": "TelegramBot"},
    )
    threads.interruptable_sleep.wait()
    log.info(
        "Terminating asyncio loop",
        extra={"shutting_down": threads.shutting_down},
    )
    loop.stop()


# -- validation helper ---------------------------------------------------------


async def validate(
    command_name: str,
    update: Update,
) -> TelegramUser | None:
    """Check the user is a real human on the allowlist.

    Returns the verified TelegramUser or None if the request should be discarded.
    """
    user: TelegramUser | None = update.effective_user
    if user is None or user.is_bot:
        log.debug("Ignoring bot user", extra={"command": command_name})
        return None
    allowed = app_config.get("telegram", "enabled_users_csv").split(",")
    if str(user.id) not in allowed:
        log.info(
            "Ignoring user not in allowlist",
            extra={"command": command_name, "user_id": user.id},
        )
        help_url = app_config.get(
            "telegram",
            "help_url",
            fallback="https://github.com/tailucas/inverter-monitor",
        )
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                text=(
                    f"{emoji.emojize(':construction:')} Sorry, you are not "
                    f"authorised. See [here]({help_url})."
                ),
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
            )
        return None
    log.info(
        "Telegram command",
        extra={
            "command": command_name,
            "user_id": user.id,
            "language": user.language_code,
        },
    )
    return user


# -- command handlers ----------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start -- introduction message."""
    if update.effective_message is None:
        return ConversationHandler.END
    user = await validate("start", update)
    if user is None:
        return ConversationHandler.END
    try:
        await update.effective_message.reply_text(
            text=(
                f"{emoji.emojize(':satellite_antenna:')} "
                f"Hello {user.first_name}! "
                f"I report on your inverter system.\n\n"
                f"Commands:\n"
                f"/status -- current inverter and battery status\n"
                f"/history [hours] -- charts of power and battery\n"
                f"/help -- this message"
            ),
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        log.warning(
            "Failed to send start message", exc_info=exc, extra={"user_id": user.id}
        )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /help -- usage info."""
    if update.effective_message is None:
        return ConversationHandler.END
    user = await validate("help", update)
    if user is None:
        return ConversationHandler.END
    try:
        await update.effective_message.reply_text(
            text=(
                f"{emoji.emojize(':light_bulb:')} **Commands**\n\n"
                f"/status -- live snapshot of solar, load, grid, battery, BMS\n"
                f"/history [hours] \u2014 time-series charts of power and battery\n\n"
                f"Examples:\n"
                f"/history 12 \u2014 last 12 hours\n"
                f"/history \u2014 default ({DEFAULT_HISTORY_HOURS} hours)"
            ),
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        log.warning(
            "Failed to send help message", exc_info=exc, extra={"user_id": user.id}
        )
    return ConversationHandler.END


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /status -- reply with buffer snapshot, then a live inverter query."""
    if update.effective_message is None:
        return ConversationHandler.END
    user = await validate("status", update)
    if user is None:
        return ConversationHandler.END
    try:
        bot: TelegramBot = context.application.bot_data.get("telegram_bot")  # type: ignore[assignment]
        if bot is None:
            raise RuntimeError("TelegramBot not registered in bot_data")

        # Step 1 -- buffered status (unchanged behaviour)
        snap = bot._buffer.snapshot()
        msg = format_status_message(snap)
        log.debug(
            "Replying to status request (buffered)",
            extra={"user_id": user.id, "data_age_secs": int(snap.age_secs)},
        )
        await update.effective_message.reply_text(
            text=msg,
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN,
        )

        # Step 2 -- live query (skipped if no callable is available)
        if bot._inverter_query is None:
            log.debug(
                "No inverter query callable; live status skipped",
                extra={"user_id": user.id},
            )
            return ConversationHandler.END

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )
        loop = asyncio.get_running_loop()
        fresh = await loop.run_in_executor(None, bot._inverter_query)

        if fresh is not None and isinstance(fresh, dict) and fresh:
            # Build a StatusSnapshot from the live data, reusing the
            # last BMS summary so battery details are still visible.
            live_snap = StatusSnapshot(
                inverter=fresh,
                bms_summary=bot._buffer.snapshot().bms_summary,
                timestamp=time.time(),
            )
            live_msg = format_status_message(live_snap)
            log.debug(
                "Replying to status request (live)",
                extra={"user_id": user.id},
            )
            await update.effective_message.reply_text(
                text=live_msg,
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            log.warning(
                "Live inverter query returned no data",
                extra={"user_id": user.id, "fresh": str(fresh)},
            )
    except Exception as exc:
        log.warning(
            "Failed to handle status command", exc_info=exc, extra={"user_id": user.id}
        )
        await update.effective_message.reply_text(
            text=(f"{emoji.emojize(':warning:')} Could not get status: {exc}"),
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN,
        )
    return ConversationHandler.END


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /history [hours] -- fetch Prometheus metrics and render charts."""
    if update.effective_message is None:
        return ConversationHandler.END
    user = await validate("history", update)
    if user is None:
        return ConversationHandler.END
    try:
        bot: TelegramBot = context.application.bot_data.get("telegram_bot")  # type: ignore[assignment]
        if bot is None:
            raise RuntimeError("TelegramBot not registered in bot_data")

        await context.bot.send_chat_action(
            chat_id=update.effective_message.chat_id,
            action=ChatAction.TYPING,
        )

        hours = DEFAULT_HISTORY_HOURS
        if context.args:
            try:
                hours = int(context.args[0])
                if hours < 1 or hours > 720:
                    hours = DEFAULT_HISTORY_HOURS
            except ValueError, IndexError:
                hours = DEFAULT_HISTORY_HOURS

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _fetch_and_render, hours)
        df_power, df_battery, img_power, img_battery = result
        caption = build_history_caption(df_power, df_battery, hours)

        if not img_power and not img_battery:
            await update.effective_message.reply_text(
                text=(
                    f"{emoji.emojize(':warning:')} No history found in "
                    f"Prometheus / Grafana Cloud."
                ),
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END

        if img_power:
            await update.effective_message.reply_photo(
                photo=img_power,
                caption=caption,
            )
        else:
            await update.effective_message.reply_text(
                text=caption,
            )

        if img_battery:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=img_battery,
            )

        log.info(
            "History report sent",
            extra={
                "user_id": user.id,
                "hours": hours,
                "has_power_chart": bool(img_power),
                "has_battery_chart": bool(img_battery),
            },
        )
    except Exception as exc:
        log.warning(
            "Failed to handle history command", exc_info=exc, extra={"user_id": user.id}
        )
        await update.effective_message.reply_text(
            text=(
                f"{emoji.emojize(':warning:')} Could not generate history report: {exc}"
            ),
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN,
        )
    return ConversationHandler.END


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Echo user-supplied text verbatim (no parse mode)."""
    if update.effective_message is None or update.effective_message.text is None:
        return ConversationHandler.END
    await update.effective_message.reply_text(text=update.effective_message.text)
    return ConversationHandler.END


async def telegram_error_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log Telegram errors gracefully."""
    log.warning(
        msg="Bot error:",
        exc_info=context.error,
        extra={"update_id": update.update_id if update else None},
    )


# -- AppThread -----------------------------------------------------------------


def _fetch_and_render(
    hours: int,
) -> tuple[pd.DataFrame, pd.DataFrame, bytes, bytes]:
    """Blocking call: fetch Prometheus metrics and render charts."""
    power_results = fetch_metrics(hours=hours, query_set=POWER_QUERIES)
    battery_results = fetch_metrics(hours=hours, query_set=BATTERY_QUERIES)

    # Build DataFrames from PrometheusMetricDTO lists.
    # _to_df pivots on metric_name so each series becomes a column.
    def _to_df(
        results: list[Any],
    ) -> pd.DataFrame:
        if not results:
            return pd.DataFrame()
        rows: dict[float, dict[str, float]] = {}
        for r in results:
            ts_key = r.ts_ms
            if ts_key not in rows:
                rows[ts_key] = {"_time": pd.Timestamp(ts_key, unit="ms")}
            rows[ts_key][r.metric_name] = r.value
        df = pd.DataFrame(list(rows.values()))
        if "_time" in df.columns:
            df = df.sort_values(by="_time")
        return df

    df_power = _to_df(power_results)
    df_battery = _to_df(battery_results)

    img_power = render_power_chart(df_power)
    img_battery = render_battery_chart(df_battery)
    return df_power, df_battery, img_power, img_battery


class TelegramBot(AppThread, Closable):
    """Runs the Telegram bot polling loop and manages the status buffer."""

    def __init__(
        self,
        creds_obj: Any,
        inverter_query: Callable[[], dict | None] | None = None,
    ) -> None:
        AppThread.__init__(self, name=self.__class__.__name__)
        # Closable PULL binds to the Telegram ZMQ endpoint; EventProcessor's
        # PUSH socket connects to it. One side must bind for inproc delivery.
        Closable.__init__(self, connect_url=URL_WORKER_TELEGRAM)
        self._creds = creds_obj
        self._token = _get_telegram_token(creds_obj)
        self._buffer = StatusBuffer()
        self._inverter_query = inverter_query
        self._receiver_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _receiver(self) -> None:
        """Background thread: bind PULL socket and ingest telemetry events."""
        log.info("Telegram receiver thread started")
        # get_socket() binds the PULL socket to URL_WORKER_TELEGRAM
        pull_socket = self.get_socket()
        while not threads.shutting_down:
            try:
                event = pull_socket.recv_pyobj()
            except zmq.ZMQError:
                break
            if not isinstance(event, dict):
                continue
            for point_name, point_items in event.items():
                if point_name == "inverter" and isinstance(point_items, dict):
                    self._buffer.update_inverter(point_items)
                elif point_name == "battery" and isinstance(point_items, list):
                    bms_summary: dict[str, Any] = {"active_count": len(point_items)}
                    for entry in point_items:
                        metrics = entry.get("metrics", {})
                        if isinstance(metrics, dict):
                            for k in (
                                "voltage_v",
                                "min_cell_v",
                                "max_cell_v",
                                "cell_diff_mv",
                            ):
                                v = metrics.get(k)
                                if v is not None:
                                    bms_summary.setdefault(k, v)
                    self._buffer.update_bms_summary(bms_summary)
        log.info("Telegram receiver thread finished")
        try:
            pull_socket.close()
        except Exception:
            pass

    def run(self) -> None:
        """Start ZMQ receiver, asyncio loop, Telegram bot, and terminator."""
        log.info("Starting Telegram bot listener")

        self._receiver_thread = threading.Thread(
            name="telegram-receiver", target=self._receiver, daemon=True
        )
        self._receiver_thread.start()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        application = Application.builder().token(self._token).build()
        application.bot_data["telegram_bot"] = self

        command_handlers = [
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("status", status),
            CommandHandler("history", history),
        ]
        for handler in command_handlers:
            application.add_handler(handler)

        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
        application.add_error_handler(callback=telegram_error_handler)  # type: ignore[arg-type]

        log.info("Telegram bot handlers registered; starting polling")

        terminator_thread = threading.Thread(
            name="telegram-terminator",
            target=terminator,
            args=(self._loop,),
            daemon=True,
        )
        terminator_thread.start()

        try:
            application.run_polling(stop_signals=None)
        except TimedOut:
            log.warning("Telegram client error.", exc_info=True)
        except Exception:
            log.warning("Telegram bot polling ended", exc_info=True)

        log.info("Telegram bot polling finished")

    def close(self) -> None:
        """Shutdown: close the asyncio loop."""
        Closable.close(self)
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
