#!/usr/bin/env python
"""Telegram bot thread and command handlers for inverter-monitor.

Follows conventions from .clinerules/telegram.md:
handler signatures, standard skeleton, parse modes, emoji, error handler,
and terminator shutdown.

Pure helper functions and data structures are in `app.telegram_bot`.
"""

import asyncio
import queue
import threading
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
    CELL_QUERIES,
    POWER_QUERIES,
    fetch_metrics,
)
from app.telegram_bot import (
    DEFAULT_HISTORY_HOURS,
    URL_WORKER_TELEGRAM,
    BmsSummaryBuffer,
    WeatherBuffer,
    _get_telegram_token,
    build_battery_caption,
    build_bms_summary,
    build_cell_recommendation,
    build_history_caption,
    build_notification_message,
    format_status_message,
    render_battery_chart,
    render_cell_chart,
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
                f"/status -- current inverter, weather and battery status\n"
                f"/history [hours] -- power time-series chart\n"
                f"/battery [hours] -- battery time-series chart\n"
                f"/cell [hours] -- per-cell voltages and balancing advice\n"
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
                f"/status -- live snapshot: inverter, battery, weather\n"
                f"/history [hours] \u2014 power time-series chart\n"
                f"/battery [hours] \u2014 battery time-series chart\n"
                f"/cell [hours] \u2014 per-cell voltages and balancing advice\n\n"
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
    """Handle /status -- live inverter query with cached BMS summary."""
    if update.effective_message is None:
        return ConversationHandler.END
    user = await validate("status", update)
    if user is None:
        return ConversationHandler.END
    try:
        bot: TelegramBot = context.application.bot_data.get("telegram_bot")  # type: ignore[assignment]
        if bot is None:
            raise RuntimeError("TelegramBot not registered in bot_data")

        if bot._inverter_query is None:
            log.warning(
                "Inverter query not available for status command",
                extra={"user_id": user.id},
            )
            await update.effective_message.reply_text(
                text=(f"{emoji.emojize(':warning:')} Inverter query is not available."),
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )
        loop = asyncio.get_running_loop()
        fresh = await loop.run_in_executor(None, bot._inverter_query)

        if fresh is not None and isinstance(fresh, dict) and fresh:
            live_msg = format_status_message(
                inverter=fresh,
                bms_summary=bot._bms_summary.summary(),
                weather=bot._weather.summary(),
            )
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
            await update.effective_message.reply_text(
                text=(
                    f"{emoji.emojize(':warning:')} Could not retrieve live status: "
                    "inverter query returned no data."
                ),
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
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


def _parse_hours(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Parse the optional hours argument, falling back to the default."""
    if context.args:
        try:
            hours = int(context.args[0])
            if 1 <= hours <= 720:
                return hours
        except ValueError, IndexError:
            pass
    return DEFAULT_HISTORY_HOURS


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

        hours = _parse_hours(context)

        loop = asyncio.get_running_loop()
        df_power, img_power = await loop.run_in_executor(
            None, _fetch_and_render_power, hours
        )
        caption = build_history_caption(df_power, hours)

        if not img_power:
            await update.effective_message.reply_text(
                text=(
                    f"{emoji.emojize(':warning:')} No power history found in "
                    f"Prometheus / Grafana Cloud."
                ),
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END

        await update.effective_message.reply_photo(
            photo=img_power,
            caption=caption,
        )

        log.info(
            "History report sent",
            extra={"user_id": user.id, "hours": hours},
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


async def battery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /battery [hours] -- battery time-series chart."""
    if update.effective_message is None:
        return ConversationHandler.END
    user = await validate("battery", update)
    if user is None:
        return ConversationHandler.END
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_message.chat_id,
            action=ChatAction.TYPING,
        )

        hours = _parse_hours(context)

        loop = asyncio.get_running_loop()
        df_battery, img_battery = await loop.run_in_executor(
            None, _fetch_and_render_battery, hours
        )
        caption = build_battery_caption(df_battery, hours)

        if not img_battery:
            await update.effective_message.reply_text(
                text=(
                    f"{emoji.emojize(':warning:')} No battery history found in "
                    f"Prometheus / Grafana Cloud."
                ),
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END

        await update.effective_message.reply_photo(
            photo=img_battery,
            caption=caption,
        )

        log.info(
            "Battery report sent",
            extra={"user_id": user.id, "hours": hours},
        )
    except Exception as exc:
        log.warning(
            "Failed to handle battery command", exc_info=exc, extra={"user_id": user.id}
        )
        await update.effective_message.reply_text(
            text=(
                f"{emoji.emojize(':warning:')} Could not generate battery report: {exc}"
            ),
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN,
        )
    return ConversationHandler.END


async def cell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cell [hours] -- per-cell voltages and balancing advice."""
    if update.effective_message is None:
        return ConversationHandler.END
    user = await validate("cell", update)
    if user is None:
        return ConversationHandler.END
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_message.chat_id,
            action=ChatAction.TYPING,
        )

        hours = _parse_hours(context)

        loop = asyncio.get_running_loop()
        df_cells, img_cells = await loop.run_in_executor(
            None, _fetch_and_render_cells, hours
        )
        caption = build_cell_recommendation(df_cells)

        if not img_cells:
            await update.effective_message.reply_text(
                text=(
                    f"{emoji.emojize(':warning:')} No cell history found in "
                    f"Prometheus / Grafana Cloud.\n\n{caption}"
                ),
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END

        await update.effective_message.reply_photo(
            photo=img_cells,
            caption=caption,
        )

        log.info(
            "Cell report sent",
            extra={"user_id": user.id, "hours": hours},
        )
    except Exception as exc:
        log.warning(
            "Failed to handle cell command", exc_info=exc, extra={"user_id": user.id}
        )
        await update.effective_message.reply_text(
            text=(
                f"{emoji.emojize(':warning:')} Could not generate cell report: {exc}"
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


def _to_df(results: list[Any], use_labels: bool = False) -> pd.DataFrame:
    """Pivot Prometheus DTOs into a DataFrame, one column per series."""
    if not results:
        return pd.DataFrame()
    rows: dict[float, dict[str, Any]] = {}
    for r in results:
        ts_key = r.ts_ms
        if ts_key not in rows:
            rows[ts_key] = {"_time": pd.Timestamp(ts_key, unit="ms")}
        if use_labels:
            labels = getattr(r, "labels", {}) or {}
            column = f"{labels.get('bms_addr', '?')} c{labels.get('cell', '?')}"
        else:
            column = r.metric_name
        rows[ts_key][column] = r.value
    df = pd.DataFrame(list(rows.values()))
    if "_time" in df.columns:
        df = df.sort_values(by="_time")
    return df


def _fetch_and_render_power(hours: int) -> tuple[pd.DataFrame, bytes]:
    """Blocking call: fetch power metrics and render the chart."""
    results = fetch_metrics(hours=hours, query_set=POWER_QUERIES)
    df = _to_df(results)
    return df, render_power_chart(df)


def _fetch_and_render_battery(hours: int) -> tuple[pd.DataFrame, bytes]:
    """Blocking call: fetch battery metrics and render the chart."""
    results = fetch_metrics(hours=hours, query_set=BATTERY_QUERIES)
    df = _to_df(results)
    return df, render_battery_chart(df)


def _fetch_and_render_cells(hours: int) -> tuple[pd.DataFrame, bytes]:
    """Blocking call: fetch per-cell voltages and render the chart."""
    results = fetch_metrics(hours=hours, query_set=CELL_QUERIES, step="1m")
    df = _to_df(results, use_labels=True)
    return df, render_cell_chart(df)


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
        self._bms_summary = BmsSummaryBuffer()
        self._weather = WeatherBuffer()
        self._inverter_query = inverter_query
        self._receiver_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._application: Application | None = None
        self._notify_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._notify_event: asyncio.Event | None = None
        self._notify_task: asyncio.Task | None = None

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
                if point_name == "battery" and isinstance(point_items, list):
                    bms_summary = build_bms_summary(point_items)
                    self._bms_summary.update(bms_summary)
                elif point_name == "weather" and isinstance(point_items, dict):
                    self._weather.update(point_items)
                elif point_name in ("switch_event", "load_alert") and isinstance(
                    point_items, dict
                ):
                    self._enqueue_notification({point_name: point_items})
        log.info("Telegram receiver thread finished")
        try:
            pull_socket.close()
        except Exception:
            pass

    # -- notification dispatch (bot-initiated messages) -----------------------

    def _enqueue_notification(self, payload: dict[str, Any]) -> None:
        """Queue a notification and wake the asyncio dispatcher, if ready."""
        self._notify_queue.put(payload)
        loop = self._loop
        notify_event = self._notify_event
        if loop is not None and notify_event is not None:
            try:
                loop.call_soon_threadsafe(notify_event.set)
            except RuntimeError:
                # the event loop is already shutting down
                pass

    async def _post_init(self, application: Application) -> None:
        """Start the notification dispatcher once PTB is initialized."""
        self._application = application
        self._loop = asyncio.get_running_loop()
        self._notify_event = asyncio.Event()
        self._notify_task = asyncio.create_task(
            self._drain_notifications(), name="telegram-notifications"
        )
        # drain anything queued before initialization
        self._notify_event.set()
        log.info("Telegram notification dispatcher started")

    async def _post_stop(self, application: Application) -> None:
        """Cancel the notification dispatcher during shutdown."""
        task = self._notify_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        log.info("Telegram notification dispatcher stopped")

    async def _drain_notifications(self) -> None:
        """Send queued notifications to allowlisted users."""
        while not threads.shutting_down:
            notify_event = self._notify_event
            if notify_event is None:
                return
            try:
                await asyncio.wait_for(notify_event.wait(), timeout=5)
            except TimeoutError:
                # fall through: drain anything queued without a wake-up
                pass
            notify_event.clear()
            while True:
                try:
                    payload = self._notify_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    await self._send_notification(payload)
                except Exception:
                    log.warning(
                        "Notification dispatch failed",
                        exc_info=True,
                        extra={"payload_keys": sorted(payload.keys())},
                    )

    async def _send_notification(self, payload: dict[str, Any]) -> None:
        """Send one notification message to every allowlisted user."""
        application = self._application
        payload_keys = sorted(payload.keys())
        if application is None:
            log.warning(
                "Telegram application is not ready; dropping notification",
                extra={"payload_keys": payload_keys},
            )
            return
        text = None
        try:
            text = build_notification_message(payload)
        except Exception:
            log.warning(
                "Failed to format notification",
                exc_info=True,
                extra={"payload_keys": payload_keys},
            )
            return
        if text is None:
            log.warning(
                "Unsupported notification payload",
                extra={"payload_keys": payload_keys},
            )
            return
        recipient_count = 0
        for user_id in app_config.get("telegram", "enabled_users_csv").split(","):
            try:
                chat_id = int(user_id)
            except ValueError:
                log.warning(
                    "Ignoring non-numeric Telegram user id",
                    extra={"user_id": user_id},
                )
                continue
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
                recipient_count += 1
            except Exception as exc:
                log.warning(
                    "Failed to send Telegram notification",
                    exc_info=exc,
                    extra={"user_id": user_id, "payload_keys": payload_keys},
                )
        log.info(
            "Telegram notification dispatched",
            extra={
                "recipient_count": recipient_count,
                "payload_keys": payload_keys,
            },
        )

    def run(self) -> None:
        """Start ZMQ receiver, asyncio loop, Telegram bot, and terminator."""
        log.info("Starting Telegram bot listener")

        self._receiver_thread = threading.Thread(
            name="telegram-receiver", target=self._receiver, daemon=True
        )
        self._receiver_thread.start()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        application = (
            Application.builder()
            .token(self._token)
            .post_init(self._post_init)
            .post_stop(self._post_stop)
            .build()
        )
        application.bot_data["telegram_bot"] = self

        command_handlers = [
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("status", status),
            CommandHandler("history", history),
            CommandHandler("battery", battery),
            CommandHandler("cell", cell),
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
