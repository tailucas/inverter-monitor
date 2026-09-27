#!/usr/bin/env python
"""Telegram bot thread and command handlers for inverter-monitor.

Follows conventions from .clinerules/telegram.md:
handler signatures, standard skeleton, parse modes, emoji, error handler,
and terminator shutdown.

Pure helper functions and data structures are in `app.telegram_bot`.
"""

import asyncio
import datetime
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
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.gemini_image import GeminiImageClient
from app.image_prompts import (
    DEFAULT_LOAD_WARNING_W,
    DEFAULT_STYLES,
    MAX_PROMPT_TOKENS,
    ImaginePrompt,
    StyleConfig,
    build_image_prompt,
    estimate_prompt_tokens,
    resolve_imagine_args,
    style_names,
    time_of_day_phase,
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
    SwitchStatsBuffer,
    WeatherBuffer,
    _get_telegram_token,
    build_battery_caption,
    build_bms_summary,
    build_cell_recommendation,
    build_history_caption,
    build_imagine_caption,
    build_imagine_prompt_message,
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
    chat = update.effective_chat
    # chat context makes it easy to discover the target room id
    chat_context = {
        "chat_id": chat.id if chat is not None else None,
        "chat_type": str(chat.type) if chat is not None else None,
        "chat_title": chat.title if chat is not None else None,
    }
    if user is None or user.is_bot:
        log.debug(
            "Ignoring bot user",
            extra={"command": command_name, **chat_context},
        )
        return None
    allowed = app_config.get("telegram", "enabled_users_csv").split(",")
    if str(user.id) not in allowed:
        log.info(
            "Ignoring user not in allowlist",
            extra={
                "command": command_name,
                "user_id": user.id,
                **chat_context,
            },
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
            **chat_context,
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
                f"/imagine [scene] -- picture of the inverter in its mood\n"
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
                f"/cell [hours] \u2014 per-cell voltages and balancing advice\n"
                f"/imagine [material] [style] \u2014 picture of the inverter\n\n"
                f"Examples:\n"
                f"/history 12 \u2014 last 12 hours\n"
                f"/history \u2014 default ({DEFAULT_HISTORY_HOURS} hours)\n"
                f"/imagine \u2014 a random style\n"
                f"`/imagine copper` \u2014 copper build, random style\n"
                f"`/imagine brushed_aluminium hyperrealistic` \u2014 style "
                f"names and material words use underscores"
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


async def imagine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /imagine [scene] -- generate a picture of the inverter."""
    if update.effective_message is None:
        return ConversationHandler.END
    user = await validate("imagine", update)
    if user is None:
        return ConversationHandler.END
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_message.chat_id,
            action=ChatAction.TYPING,
        )

        bot: TelegramBot = context.application.bot_data.get(  # type: ignore[assignment]
            "telegram_bot"
        )
        if bot is None:
            raise RuntimeError("TelegramBot not registered in bot_data")
        if bot._image_client is None:
            await update.effective_message.reply_text(
                text=(
                    f"{emoji.emojize(':warning:')} Image generation is not configured."
                ),
            )
            return ConversationHandler.END

        last_choices = bot.imagine_last_choices
        resolved = resolve_imagine_args(
            context.args,
            avoid_style=last_choices["style"],
            avoid_location=last_choices["location"],
            avoid_material=last_choices["material"],
        )
        if (
            resolved.error is not None
            or resolved.style is None
            or resolved.location is None
        ):
            available = ", ".join(style_names(DEFAULT_STYLES))
            if resolved.error == "too_many_args":
                message = (
                    f"{emoji.emojize(':warning:')} Too many arguments. Use "
                    f"/imagine [material] [style] -- styles: {available}. "
                    f"Join material words with underscores."
                )
            elif resolved.error == "invalid_material":
                message = (
                    f"{emoji.emojize(':warning:')} Material must be a short "
                    f"phrase of words or numbers joined by underscores. "
                    f"Styles: {available}."
                )
            else:
                message = (
                    f"{emoji.emojize(':warning:')} Unknown style. Available "
                    f"styles: {available}. The location is always outdoors."
                )
            await update.effective_message.reply_text(text=message)
            return ConversationHandler.END
        style = resolved.style
        location = resolved.location
        # the prompt header shows these choices even if the render fails, so
        # record them synchronously and keep the next rotation varied
        bot.remember_imagine_choices(
            style=style.name,
            location=location,
            material=resolved.material,
        )

        loop = asyncio.get_running_loop()
        request = await loop.run_in_executor(
            None, bot.build_imagine, style, location, resolved.material
        )
        prompt_tokens = estimate_prompt_tokens(request.prompt)
        await update.effective_message.reply_html(
            text=build_imagine_prompt_message(
                style.name,
                style.aspect_ratio,
                request.prompt,
                prompt_tokens,
                MAX_PROMPT_TOKENS,
                material=resolved.material,
                time_of_day=request.time_of_day,
            ),
        )

        await context.bot.send_chat_action(
            chat_id=update.effective_message.chat_id,
            action=ChatAction.TYPING,
        )
        image_bytes = await loop.run_in_executor(None, bot.render_imagine, request)
        await update.effective_message.reply_photo(
            photo=image_bytes,
            caption=request.caption,
        )

        log.info(
            "Imagine report sent",
            extra={
                "user_id": user.id,
                "style": style.name,
                "material": resolved.material,
                "time_of_day": request.time_of_day,
                "model": bot._image_client.model,
                "aspect_ratio": style.aspect_ratio,
                "prompt_chars": len(request.prompt),
                "prompt_tokens_estimate": prompt_tokens,
                "image_bytes": len(image_bytes),
            },
        )
    except Exception as exc:
        log.warning(
            "Failed to handle imagine command", exc_info=exc, extra={"user_id": user.id}
        )
        await update.effective_message.reply_text(
            text=f"{emoji.emojize(':warning:')} Could not generate image: {exc}"
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


# -- notification helpers ------------------------------------------------------

# Telegram rate-limit hints longer than this are logged but not waited out.
MAX_TELEGRAM_RETRY_WAIT_SECONDS = 30


def _is_permanent_recipient_error(exc: Exception) -> bool:
    """True when a Telegram send failure cannot recover without user action.

    ``Forbidden`` covers "bot can't initiate conversation with a user" and
    "bot was blocked by the user"; ``BadRequest: chat not found`` means the
    recipient never started the bot (or the ID is stale).
    """
    if isinstance(exc, Forbidden):
        return True
    if isinstance(exc, BadRequest):
        return "chat not found" in str(exc).lower()
    return False


def _notification_chat_ids() -> list[str]:
    """Chat IDs used for bot-initiated notifications.

    A configured ``[telegram] chat_room_id`` (a group the bot has joined)
    takes precedence: group rooms are an existing chat, so Telegram's
    "bot can't initiate conversation with a user" restriction does not
    apply. Without it, the allowlisted user IDs are used, each of which must
    have started the bot.
    """
    chat_room_id = app_config.get("telegram", "chat_room_id", fallback="").strip()
    if chat_room_id:
        try:
            int(chat_room_id)
        except ValueError:
            log.warning(
                "Ignoring non-numeric Telegram chat room id",
                extra={"chat_room_id": chat_room_id},
            )
        else:
            return [chat_room_id]
    return app_config.get("telegram", "enabled_users_csv").split(",")


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
        self._switch_stats = SwitchStatsBuffer()
        self._load_warning_w = app_config.getint(
            "alert_thresholds", "load_warning_w", fallback=DEFAULT_LOAD_WARNING_W
        )
        self._load_shed_w = app_config.getint(
            "alert_thresholds", "load_shed_w", fallback=self._load_warning_w
        )
        # image generation is optional: a missing Gemini credential must not
        # stop the rest of the bot from working
        self._image_client: GeminiImageClient | None = None
        try:
            self._image_client = GeminiImageClient(creds_obj)
        except Exception:
            log.warning(
                "Gemini image generation is unavailable; /imagine is disabled",
                exc_info=True,
            )
        self._inverter_query = inverter_query
        # the choices shown in the last picture, so the next /imagine
        # rotation cannot repeat them
        self._imagine_last_choices: dict[str, str | None] = {
            "style": None,
            "location": None,
            "material": None,
        }
        self._receiver_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._application: Application | None = None
        self._notify_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._notify_event: asyncio.Event | None = None
        self._notify_task: asyncio.Task | None = None
        self._unreachable_users: set[int] = set()

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
                elif point_name == "switches" and isinstance(point_items, dict):
                    self._switch_stats.update(point_items)
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
        chat_room_id = app_config.get("telegram", "chat_room_id", fallback="").strip()
        log.info(
            "Telegram notification destinations resolved",
            extra={
                "chat_room_id": chat_room_id or None,
                "recipient_count": len(_notification_chat_ids()),
            },
        )
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

        async def _send(chat_id: int) -> None:
            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )

        recipient_count = 0
        unreachable_count = 0
        for chat_id_field in _notification_chat_ids():
            try:
                chat_id = int(chat_id_field)
            except ValueError:
                log.warning(
                    "Ignoring non-numeric Telegram chat id",
                    extra={"chat_id": chat_id_field},
                )
                continue
            if chat_id in self._unreachable_users:
                unreachable_count += 1
                log.debug(
                    "Skipping unavailable Telegram recipient",
                    extra={"chat_id": chat_id_field, "payload_keys": payload_keys},
                )
                continue
            try:
                try:
                    await _send(chat_id)
                except RetryAfter as exc:
                    # rate limit: wait out the hint (bounded) and retry once
                    retry_after = exc.retry_after
                    retry_after_secs = (
                        retry_after.total_seconds()
                        if isinstance(retry_after, datetime.timedelta)
                        else float(retry_after)
                    )
                    wait_secs = min(retry_after_secs, MAX_TELEGRAM_RETRY_WAIT_SECONDS)
                    log.warning(
                        "Telegram rate limit; deferring notification",
                        extra={
                            "chat_id": chat_id_field,
                            "retry_after_seconds": retry_after_secs,
                            "wait_seconds": wait_secs,
                            "payload_keys": payload_keys,
                        },
                    )
                    await asyncio.sleep(wait_secs)
                    await _send(chat_id)
                recipient_count += 1
            except Exception as exc:
                error_type = f"{type(exc).__module__}.{type(exc).__name__}"
                if _is_permanent_recipient_error(exc):
                    # one actionable warning, then mute for this process
                    self._unreachable_users.add(chat_id)
                    log.warning(
                        "Telegram recipient cannot receive notifications; muting",
                        extra={
                            "chat_id": chat_id_field,
                            "error_type": error_type,
                            "error": str(exc),
                            "payload_keys": payload_keys,
                        },
                    )
                    continue
                log.warning(
                    "Failed to send Telegram notification",
                    exc_info=exc,
                    extra={
                        "chat_id": chat_id_field,
                        "error_type": error_type,
                        "payload_keys": payload_keys,
                    },
                )
        log.info(
            "Telegram notification dispatched",
            extra={
                "recipient_count": recipient_count,
                "unreachable_count": unreachable_count,
                "payload_keys": payload_keys,
            },
        )

    # -- /imagine helpers ------------------------------------------------------

    @property
    def imagine_last_choices(self) -> dict[str, str | None]:
        """The style, location and material shown in the last picture."""
        return dict(self._imagine_last_choices)

    def remember_imagine_choices(
        self, *, style: str, location: str, material: str | None
    ) -> None:
        """Record the choices shown to the user for the next rotation."""
        self._imagine_last_choices["style"] = style
        self._imagine_last_choices["location"] = location
        self._imagine_last_choices["material"] = material

    def build_imagine(
        self,
        style: StyleConfig,
        location: str,
        material: str | None = None,
        now: float | None = None,
    ) -> ImaginePrompt:
        """Blocking: snapshot telemetry and compose the image prompt."""
        inverter = None
        if self._inverter_query is not None:
            try:
                inverter = self._inverter_query()
            except Exception:
                log.warning("Live inverter query failed for /imagine", exc_info=True)
        weather = self._weather.summary()
        switches = self._switch_stats.summary()
        phase = time_of_day_phase(weather, now)
        prompt = build_image_prompt(
            inverter=inverter,
            bms=self._bms_summary.summary(),
            weather=weather,
            switches=switches,
            style=style,
            location=location,
            load_warning_w=self._load_warning_w,
            load_shed_w=self._load_shed_w,
            material=material,
            now=now,
        )
        caption = build_imagine_caption(
            inverter,
            weather,
            switches,
            load_warning_w=self._load_warning_w,
            load_shed_w=self._load_shed_w,
        )
        log.debug(
            "Composed image prompt",
            extra={
                "style": style.name,
                "material": material,
                "time_of_day": phase,
                "prompt": prompt,
                "prompt_chars": len(prompt),
                "prompt_tokens_estimate": estimate_prompt_tokens(prompt),
            },
        )
        return ImaginePrompt(
            prompt=prompt,
            caption=caption,
            style=style,
            time_of_day=phase,
        )

    def render_imagine(self, request: ImaginePrompt) -> bytes:
        """Blocking: generate the image for a composed prompt."""
        if self._image_client is None:
            raise RuntimeError("image generation is not configured")
        return self._image_client.generate_image(
            request.prompt,
            aspect_ratio=request.style.aspect_ratio,
            image_size=request.style.image_size,
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
            CommandHandler("imagine", imagine),
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
