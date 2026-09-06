---
paths:
  - "app/**"
  - "pyproject.toml"
  - "ruff.toml"
---

# Python Telegram Bot & asyncio Project Conventions

This project follows the `tailucas_pylib` framework pattern used across multiple Telegram bot implementations (e.g., `net-tool`, `investec-my-charges`). All new bot implementations MUST adhere to these conventions.

> **Logging:** all logging in this project is structured (static message +
> `extra` fields). Interpolated log messages are banned — see
> `.clinerules/logging.md`.

---

## 1. Project Entry Point (`app/__main__.py`)

### Mandatory Boot Sequence

Every `main()` function must follow this exact order:

```python
def main() -> None:
    # 1. Validate credentials FIRST
    creds = Creds()
    creds.validate_creds()

    # 2. Reduce Sentry noise immediately
    ignore_logger("telegram.ext.Updater")
    ignore_logger("telegram.ext._updater")
    ignore_logger("asyncio")

    # 3. Set log level to DEBUG
    log.setLevel(logging.DEBUG)

    # 4. Init database as first async operation
    asyncio.run(db_startup())

    # 5. Extract credentials from cred store BEFORE event loop
    # (Pull all needed creds into local variables)

    # 6. Configure metrics/subsystems BEFORE Application
    metrics_configure(url=..., user=..., token=...)

    # 7. Build and configure Application
    application = Application.builder().token(...).build()
    # register handlers, add error handler

    # 8. Run polling inside try/finally
    try:
        application.run_polling()
    except TimedOut:
        log.warning("Telegram client error.", exc_info=True)
    finally:
        die()
    bye()
```

### Graceful Shutdown

Always include a `terminator` function wired to `threads.interruptable_sleep`:

```python
def terminator(loop: AbstractEventLoop) -> None:
    log.debug("asyncio loop terminator is ready.")
    threads.interruptable_sleep.wait()
    log.info(
        "Terminating asyncio loop",
        extra={"shutting_down": threads.shutting_down},
    )
    loop.stop()
```

> Logging is always structured (static message + `extra` fields).
> See `.clinerules/logging.md` for the full standard.

### Imports from `tailucas_pylib`

```python
from tailucas_pylib import threads
from tailucas_pylib.threads import die, bye
from tailucas_pylib import APP_NAME, app_config, log
from tailucas_pylib.creds import Creds
```

`APP_NAME` is injected automatically by the pylib init system. Do NOT hardcode it.

---

## 2. Telegram Bot Handlers (`app/bot.py`)

### Handler Function Signature

Every command handler must use:

```python
async def handler_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
```

Return `ConversationHandler.END` from terminal handlers.

### Standard Handler Skeleton

```python
async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None:
        return ConversationHandler.END

    await context.bot.send_chat_action(
        chat_id=update.effective_message.chat_id, action=ChatAction.TYPING
    )

    try:
        # Do work...
        result = await some_rpc_call(...)
        caption = f"{emoji.emojize(':check_mark:')} Success: {result}"
    except Exception as exc:
        log.warning("Operation failed", exc_info=exc)
        caption = f"{emoji.emojize(':warning:')} Error: {exc}"

    await update.effective_message.reply_text(
        text=caption,
        disable_web_page_preview=True,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END
```

### Response Method & Parse Mode

Governed by **Section 4 — Message Formatting & Parse Modes** (mandatory). In short:

- **Default:** `reply_text(text=..., disable_web_page_preview=True, parse_mode=ParseMode.MARKDOWN)`
- **Buttons:** any `InlineKeyboardMarkup` ⇒ `reply_html(text=..., reply_markup=...)`
- **Photos:** `reply_photo(photo=img_bytes, caption=...)` — plain captions, no parse mode
- **Never:** `telegram.helpers.escape_markdown` / `ParseMode.MARKDOWN_V2`

### Emoji Convention

Always use `emoji.emojize(':name:')` for user-facing messages. Never use raw Unicode emoji.

Common icons:
- `:warning:` — errors/unavailable
- `:check_mark:` — success
- `:globe_with_meridians:` — network
- `:satellite_antenna:` — connectivity
- `:light_bulb:` — help/info

### Global Error Handler

Always register a global error handler on the Application:

```python
async def telegram_error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.warning(msg="Bot error:", exc_info=context.error)

application.add_error_handler(callback=telegram_error_handler)  # type: ignore[arg-type]
```

### Handler Registration in `main()` (Simple Commands)

For simple, single-step commands (no multi-step dialog):

```python
command_handlers = [
    CommandHandler("command_name", handler_function),
    # ...
]
for handler in command_handlers:
    application.add_handler(handler)

# Echo handler for non-commands
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
```

For multi-step dialogs with buttons, see **Section 3**.

### Handler Imports

```python
from telegram import Update, User as TelegramUser, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction, ChatType
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
```

---

## 3. ConversationHandler: Multi-Step Dialogs

Multi-step dialogs (e.g., settings forms, confirmation flows) use `ConversationHandler` with `CallbackQueryHandler` fallbacks for inline button interactions. **Getting the wiring wrong silently breaks all button interactions inside the conversation.** This section captures the three interacting parts: action constants, the bot-side dialog logic, and the main-side wiring.

### 3a. Integer Action Constants

Define module-level integer constants for all states and actions. **Never use strings** for callback data routing — integers are unambiguous and work reliably with regex `pattern` matching.

```python
# State identifiers (returned by handlers to enter that state)
ACTION_NONE = 0
ACTION_SETTINGS = 1
ACTION_SETTINGS_UPDATE = 20

# Action identifiers (used in callback_data for InlineKeyboardButton)
ACTION_SETTINGS_PAY_DAY = 21
ACTION_SETTINGS_RESET = 23
```

### 3b. Conversation Wiring Pattern (in `__main__.py`)

The `ConversationHandler` must be constructed with `fallbacks` that include **all** `CallbackQueryHandler` instances that can fire from buttons within the conversation. The `ConversationHandler` itself must be registered **before** the individual `command_handlers`.

```python
# 1. Build command_handlers list containing ALL handlers:
#    - CommandHandler for the entry point command
#    - CallbackQueryHandler for EVERY inline button action
#    - CallbackQueryHandler for cancel/abort
command_handlers = [
    CommandHandler("settings", settings),
    CallbackQueryHandler(callback=askpayday, pattern="^" + str(ACTION_SETTINGS_PAY_DAY) + "$"),
    CallbackQueryHandler(callback=resetdefault, pattern="^" + str(ACTION_SETTINGS_RESET) + "$"),
    CallbackQueryHandler(callback=cancel, pattern="^" + str(ACTION_NONE) + ".*$"),
    # ... other CommandHandler and CallbackQueryHandler entries
]

# 2. Construct ConversationHandler using command_handlers AS ITS FALLBACKS
settings_handler = ConversationHandler(
    allow_reentry=True,
    entry_points=[CommandHandler("settings", settings)],
    states={
        ACTION_SETTINGS_UPDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_settings)],
    },
    fallbacks=command_handlers  # ← CRITICAL: all callback handlers must be in fallbacks
)

# 3. CRITICAL ORDER: ConversationHandler FIRST, then individual handlers
application.add_handler(settings_handler)
for handler in command_handlers:
    application.add_handler(handler)
```

**Common mistakes to avoid:**
- A `CallbackQueryHandler` defined in `command_handlers` but NOT also in `fallbacks` — button presses during the conversation are silently ignored
- Adding the `ConversationHandler` AFTER individual handlers — the conversation never activates
- Using string `pattern` values like `"settings"` instead of integer constants — ambiguous matching

### 3c. Multi-Step Dialog Flow (in `bot.py`)

A multi-step dialog follows this pattern:

**Step 1 — Entry handler returns a STATE (not END):** The entry point (`/settings`) presents options and returns the state constant to wait for user input.

```python
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user: TelegramUser = update.effective_user
    # Validate user, fetch current settings...
    user_keyboard = [
        [InlineKeyboardButton("Set Pay Day", callback_data=ACTION_SETTINGS_PAY_DAY)],
        [InlineKeyboardButton("Cancel", callback_data=f"{ACTION_NONE}:No changes made.")],
    ]
    reply_markup = InlineKeyboardMarkup(user_keyboard)
    await update.message.reply_html(
        text=f"{emoji.emojize(':gear:')} Current settings: ...",
        reply_markup=reply_markup,
    )
    return ACTION_SETTINGS_UPDATE  # ← returns STATE, not END
```

**Step 2 — Callback handlers in fallbacks prime context.user_data and return the STATE again:**

```python
async def askpayday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()  # ← ALWAYS answer callback queries FIRST
    # Store intent in context.user_data for the state handler to read
    context.user_data["save_pay_day"] = {"min": 1, "max": 28}
    await query.edit_message_text(
        text="Enter a day of the month (1-28):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ACTION_SETTINGS_UPDATE  # ← re-enter the waiting state
```

**Step 3 — State handler consumes context.user_data and returns END:**

```python
async def update_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None:
        return ConversationHandler.END
    user_input: str = update.message.text
    # Read which setting to update from context.user_data
    if "save_pay_day" in context.user_data:
        # validate input, persist setting...
        del context.user_data["save_pay_day"]
    await update.message.reply_text("Settings updated.")
    return ConversationHandler.END
```

**Step 4 — Cancel handler with colon-separated payload:**

```python
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # Parse colon-separated payload: "0:No changes made." → feedback
    feedback = query.data.split(":")[1] if ":" in query.data else None
    if feedback:
        await context.bot.edit_message_text(
            text=feedback,
            chat_id=update.effective_chat.id,
            message_id=update.effective_message.id,
        )
    else:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=update.effective_message.id,
        )
    return ConversationHandler.END
```

### 3d. CallbackQueryHandler Conventions

| Rule | Rationale |
|------|-----------|
| Always call `await query.answer()` as the first statement | Telegram requires acknowledgement; clients may hang without it |
| Use integer `callback_data` with regex `pattern` matching | Unambiguous; `"^" + str(ACTION_X) + "$"` for exact match, `"^" + str(ACTION_X) + ".*$"` for prefix match |
| Colon-separated payloads for data passing | `f"{ACTION_ID}:{param1}:{param2}"` parsed with `query.data.split(":")` |
| Use `query.edit_message_text()` to update the existing message in-place | Replaces button menus with results without cluttering the chat |
| Delete or edit messages in cancel/abort handlers | Prevents stale button menus from lingering |

---

## 4. Message Formatting & Parse Modes

Reference implementation: `investec-my-charges` `app/bot.py` (see `validate`, `card_report`, `update_settings`). These rules are **mandatory** for every message the bot sends. A wrong `parse_mode` decision either renders markup literally (users see `*` and backticks) or raises `BadRequest: can't parse entities`.

### 4a. Send-Method Decision Table

| Situation | Call | Parse mode |
|---|---|---|
| **Default:** any formatted text reply | `reply_text(text=..., disable_web_page_preview=True, parse_mode=ParseMode.MARKDOWN)` | `MARKDOWN` |
| Message carrying an `InlineKeyboardMarkup` | `reply_html(text=..., reply_markup=...)` | `HTML` (implicit) |
| Edit that attaches a new keyboard | `query.edit_message_text(text=..., parse_mode=ParseMode.HTML, reply_markup=...)` | `HTML` |
| Edit with progress/result text (no keyboard) | `query.edit_message_text(text=..., parse_mode=ParseMode.MARKDOWN)` | `MARKDOWN` |
| Photo/plot result (PNG bytes) | `reply_photo(photo=img_bytes, caption=caption)` or `context.bot.send_photo(chat_id=..., photo=..., caption=...)` | none — captions stay plain |
| Bot-initiated message (no user Update, e.g. event push) | `context.bot.send_message(chat_id=..., text=..., parse_mode=ParseMode.HTML)` | `HTML` |
| Trivial confirmation with no markup | `reply_text("Settings updated.")` | none |
| Echo of user-supplied text | `reply_text(update.message.text)` | none — **never parse user input** |
| Rejection of invalid user input | `reply_markdown(text=..., reply_to_message_id=update.message.id)` | `MARKDOWN` |

### 4b. Rule 1 — Default to Markdown via Explicit `parse_mode`

The usual case for a user-facing reply (from `validate`):

```python
message = rf"{emoji.emojize(':construction:')} {user.first_name}, you can express interest [here]({help_url})."
await update.message.reply_text(
    text=message,
    # do not render the summary
    disable_web_page_preview=True,
    parse_mode=ParseMode.MARKDOWN,
)
```

- `parse_mode=ParseMode.MARKDOWN` is the critical piece — it enables `*bold*`, `_italic_`, `` `code` `` and `[link text](url)`. Compose messages assuming it is on.
- `disable_web_page_preview=True` is always set; it is mandatory whenever the message contains a URL so links do not spawn preview cards.
- `reply_markdown(text=...)` is acceptable shorthand only for a simple Markdown reply that needs no other kwargs (reference: `accounts`, `update_settings`). When in doubt, use the explicit form above. Never use `reply_markdown_v2`.

### 4c. Rule 2 — Never `escape_markdown`, Never `MARKDOWN_V2`

- **Never import or use `telegram.helpers.escape_markdown`.** Blanket-escaping a composed message is fragile: it destroys intentional markup and double-escapes data.
- **Never use `ParseMode.MARKDOWN_V2` / `reply_markdown_v2`.** V2 reserves `.`, `!`, `-`, `(`, `)`, `_`, `*`, `[`, `]`, `~`, `` ` ``, `>`, `#`, `+`, `=`, `|`, `{`, `}` — practically guaranteeing parse failures on real-world text. Legacy `ParseMode.MARKDOWN` reserves only `*`, `_`, `` ` ``, `[`.
- The correct alternative is Rule 6 (9g): compose intentional Markdown and sanitize only the external data going into it.

### 4d. Rule 3 — `InlineKeyboardMarkup` ⇒ `reply_html`

Any message carrying an `InlineKeyboardMarkup` MUST be sent with `reply_html`:

```python
user_keyboard = [
    [
        InlineKeyboardButton("Authorize", callback_data=str(ACTION_AUTHORIZE)),
        InlineKeyboardButton("Cancel", callback_data=str(ACTION_NONE)),
    ]
]
await update.message.reply_html(text=user_response, reply_markup=InlineKeyboardMarkup(user_keyboard))
```

- Same for edits that attach a keyboard: `query.edit_message_text(text=..., parse_mode=ParseMode.HTML, reply_markup=...)`.
- Rationale: menu messages interpolate arbitrary text (names, labels); HTML tolerates the `_` and `*` characters common in free text that would break Markdown parsing.
- Any message containing HTML tags — including `<tg-emoji emoji-id="...">` custom-emoji tags, `<b>`, `<i>` — must use HTML parse mode end-to-end. Never mix syntaxes in one message: no `<b>` in a MARKDOWN message, no `*bold*` in an HTML message.

### 4e. Rule 4 — Callback-Query Progress Pattern

For callbacks that trigger slow work, follow the acknowledge → progress → result sequence:

```python
query = update.callback_query
await query.answer()  # always first
await query.edit_message_text(
    text=f"{emoji.emojize(':hourglass_not_done:')}", parse_mode=ParseMode.MARKDOWN
)
await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
# ... do the work ...
await query.edit_message_text(text=result_text, parse_mode=ParseMode.MARKDOWN)
```

**Exception — photo results:** a text message cannot be edited into a photo. Delete the progress message, then send the photo fresh:

```python
await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.effective_message.id)
await context.bot.send_photo(chat_id=update.effective_chat.id, photo=img_bytes, caption=caption)
```

### 4f. Rule 5 — Photos and Captions

- Send generated images as PNG bytes: `reply_photo(photo=img_bytes, caption=caption)` (or `context.bot.send_photo(chat_id=..., ...)` when replacing a progress message).
- Captions are short, plain summaries. Do NOT set a `parse_mode` on captions and do not put markup in them.

### 4g. Rule 6 — Normalize External Data; Never Escape the Whole Message

External strings (API responses, DB values, user input) must be normalized **at build time**, per target parse mode:

- Markdown messages: strip/replace Markdown metacharacters in the data — reference: `tran_detail.title().replace("*", " ")`.
- HTML messages: entity-encoded API data goes through `html.unescape(...)` before embedding — reference: `<i>{html.unescape(merchant_name)}</i>`.
- Wrap dynamic values (dates, amounts, hostnames) in backticks and labels in `*...*`; build multi-line messages as a list joined with `"\n".join(lines)`:

```python
messages = [f"Since `{start_date}`:"]
for tran_detail, tran_amnt in costs.items():
    messages.append(f"`{locale.currency(tran_amnt)}` *{tran_detail}*")
await query.edit_message_text(text="\n".join(messages), parse_mode=ParseMode.MARKDOWN)
```

### 4h. Rule 7 — When NOT to Parse

Send with NO `parse_mode` only when the text has no intended markup:

- Trivial confirmations: `await update.message.reply_text("Settings updated.")`
- Echoing user input verbatim: `await update.message.reply_text(update.message.text)` — user text must never be parsed (it can contain arbitrary markup characters).
- Cancel/backchat edits in `cancel`.

Conversely, if the text contains markup characters (backticks around a hostname, `*bold*`), a plain `reply_text` is a bug — the user sees the literal characters. Every send is an explicit parse-mode decision.

### 4i. Anti-Patterns (Banned)

```python
# BANNED: blanket escaping + MarkdownV2 (fragile; breaks on . ! - ( ) ...)
await update.effective_message.reply_markdown_v2(text=escape_markdown(caption, version=2))

# BANNED: markup composed but never rendered (user sees literal backticks)
await update.message.reply_text(text=f"Enabled test for `{remote_host}`.")

# BANNED: parsing user-supplied text as Markdown
await update.message.reply_text(text=user_text, parse_mode=ParseMode.MARKDOWN)

# BANNED: keyboard attached to a Markdown/plain send — use reply_html
await update.message.reply_markdown(text=text, reply_markup=reply_markup)
```
