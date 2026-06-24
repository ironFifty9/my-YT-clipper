"""
core/telegram.py — Telegram Bot API helpers.

Isolates every outbound Telegram API call so that:
  - Unit tests can mock these functions without patching the network
  - A future migration to python-telegram-bot or aiogram only touches this file
  - Timeout values are defined once and are easy to tune

All three functions follow the same error-handling contract:
  - Network/HTTP errors are caught and logged; they never propagate as exceptions.
  - tg_send()          returns a dict so the caller can inspect message_id.
  - tg_edit()          returns None; a failed edit is non-fatal (progress bar miss).
  - tg_send_document() returns bool; the caller decides whether to raise.

Telegram API reference: https://core.telegram.org/bots/api
"""

import logging  # for structured error/warning logs
import requests # for all outbound HTTP calls to api.telegram.org

from config import TELEGRAM_API_URL

log = logging.getLogger(__name__)   # scoped logger: "core.telegram"

# ── Timeouts ───────────────────────────────────────────────────────────────────
# Telegram's servers normally respond to message operations in < 3 seconds.
# We allow 15 s to handle momentary network hiccups without hanging the thread.
_MSG_TIMEOUT = 15    # seconds — used by tg_send() and tg_edit()

# File uploads take significantly longer because the entire clip file
# must be transferred. 120 s gives headroom for clips up to the 50 MB limit
# on typical Railway/Render network connections (~3 MB/s upstream).
_UPLOAD_TIMEOUT = 120  # seconds — used by tg_send_document()


def tg_send(bot_token: str, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
    """
    Send a new message to a Telegram chat and return the full API response.

    This is called once per job when the /clip request arrives, to create the
    initial "🎬 Processing…" status message. The returned message_id is then
    passed to tg_edit() for live progress updates.

    Args:
        bot_token: The Telegram Bot API token (e.g. "123456:ABCdef...").
        chat_id:   Telegram chat ID as a string (e.g. "987654321").
        text:      Message text. Markdown formatting is enabled.
        reply_markup: Optional inline keyboard or other reply markup dict.

    Returns:
        The parsed JSON response from Telegram on success, e.g.:
          {"ok": True, "result": {"message_id": 42, ...}}
        On network failure returns:
          {"ok": False, "error": "<exception message>"}
    """
    try:
        payload = {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "Markdown",   # allows *bold*, _italic_, `code`
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        r = requests.post(
            f"{TELEGRAM_API_URL}/bot{bot_token}/sendMessage",
            json=payload,
            timeout=_MSG_TIMEOUT,
        )
        return r.json()   # let the caller check r.json()["result"]["message_id"]
    except requests.RequestException as e:
        # Log and return a normalised error dict so callers don't need
        # to handle both dict and exception paths.
        log.error("tg_send failed: %s", e)
        return {"ok": False, "error": str(e)}


def tg_edit(
    bot_token: str,
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    """
    Edit an existing Telegram message in place.

    Used to update the "🎬 Processing…" status message with live progress bars
    as the job moves through its four pipeline steps. Each step overwrites the
    same message instead of sending new ones, keeping the chat clean.

    Args:
        bot_token:  The Telegram Bot API token.
        chat_id:    Telegram chat ID as a string.
        message_id: The integer ID of the message to update (from tg_send result).
        text:       New message content. Markdown formatting is enabled.
        reply_markup: Optional inline keyboard or other reply markup dict.

    Returns:
        None. A failed edit is treated as non-fatal — if the edit fails
        (e.g. Telegram rate-limit or network blip), the user simply misses
        a progress update but the job continues running normally.
    """
    try:
        payload = {
            "chat_id":    chat_id,
            "message_id": message_id,
            "text":       text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        r = requests.post(
            f"{TELEGRAM_API_URL}/bot{bot_token}/editMessageText",
            json=payload,
            timeout=_MSG_TIMEOUT,
        )
        # Log non-2xx responses as warnings (not errors) because a failed
        # edit does not block the pipeline.
        if not r.ok:
            log.warning("tg_edit HTTP %s: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("tg_edit network error: %s", e)


def tg_send_document(
    bot_token: str,
    chat_id: str,
    file_path: str,
    caption: str = "",
) -> bool:
    """
    Upload a file to a Telegram chat using the sendDocument endpoint.

    The file is streamed directly from disk using multipart/form-data, so the
    full file contents must be present at `file_path` before calling this function.

    Args:
        bot_token:  The Telegram Bot API token.
        chat_id:    Telegram chat ID as a string.
        file_path:  Absolute or relative path to the file to upload.
        caption:    Optional caption shown below the file in the chat.
                    Markdown formatting is enabled.

    Returns:
        True  — if Telegram responded with HTTP 2xx (upload accepted).
        False — if the request failed (network error or non-2xx HTTP status).

    Important: this function only *logs* errors; it never raises.
    The caller (core/worker.py) is responsible for checking the return value
    and raising an appropriate exception if False is returned.
    """
    try:
        # Open in binary read mode — required for multipart file upload.
        # The `with` block ensures the file handle is closed even on error.
        with open(file_path, "rb") as fh:
            r = requests.post(
                f"{TELEGRAM_API_URL}/bot{bot_token}/sendDocument",
                # Text fields go in `data`, the file goes in `files`.
                # They cannot be combined in `json=` because of the binary file.
                data={
                    "chat_id":    chat_id,
                    "caption":    caption,
                    "parse_mode": "Markdown",
                },
                files={"document": fh},   # "document" is the Telegram API field name
                timeout=_UPLOAD_TIMEOUT,
            )
        if not r.ok:
            # Log the full Telegram error body (truncated to 500 chars)
            # so it appears in Railway/Render logs for debugging.
            log.error(
                "tg_send_document HTTP %s: %s", r.status_code, r.text[:500]
            )
        return r.ok   # True = success, False = upload rejected

    except requests.RequestException as e:
        # Network-level failure (timeout, DNS error, connection reset, etc.)
        log.error("tg_send_document network error: %s", e)
        return False  # caller will raise RuntimeError and notify the user


def tg_set_webhook(bot_token: str, webhook_url: str) -> bool:
    """
    Configure Telegram webhook for the bot.
    """
    try:
        r = requests.post(
            f"{TELEGRAM_API_URL}/bot{bot_token}/setWebhook",
            json={"url": webhook_url},
            timeout=_MSG_TIMEOUT,
        )
        if r.ok:
            log.info("Telegram webhook successfully set to: %s", webhook_url)
            return True
        log.error("Failed to set Telegram webhook: HTTP %s: %s", r.status_code, r.text[:500])
        return False
    except requests.RequestException as e:
        log.error("tg_set_webhook network error: %s", e)
        return False


def tg_answer_callback_query(bot_token: str, callback_query_id: str, text: str | None = None) -> None:
    """
    Acknowledge Telegram callback queries to dismiss the loading state on the button.
    """
    try:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        r = requests.post(
            f"{TELEGRAM_API_URL}/bot{bot_token}/answerCallbackQuery",
            json=payload,
            timeout=_MSG_TIMEOUT,
        )
        if not r.ok:
            log.warning("tg_answer_callback_query HTTP %s: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("tg_answer_callback_query network error: %s", e)
