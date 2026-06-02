"""Deliver a message AS the owner (via business_connection_id), or route it
to the owner for one-tap approval when running in approve mode / on escalation.
"""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.db import local
from bot.utils.keyboards import draft_kb

logger = logging.getLogger(__name__)


async def deliver(bot: Bot, conn_id: str, chat_id: int, text: str,
                  *, typing: bool = True) -> tuple[bool, str | None]:
    """Send `text` into `chat_id` on behalf of the owner's account.

    Works only where the owner already has a chat with that user — Telegram
    blocks cold-starting a conversation from a personal account.
    """
    try:
        if typing:
            try:
                await bot.send_chat_action(
                    chat_id=chat_id, action="typing", business_connection_id=conn_id
                )
                await asyncio.sleep(1.2)
            except TelegramBadRequest:
                pass
        # The model is told to format with Telegram HTML (<b>, <i>, …), so we
        # send under the bot's global HTML default. If it slips and emits a
        # stray '<'/'&' or a broken tag, Telegram rejects the message with a
        # parse error — fall back to sending the text verbatim as plain text so
        # the contact still gets the reply.
        try:
            await bot.send_message(
                chat_id=chat_id, text=text, business_connection_id=conn_id,
            )
        except TelegramBadRequest as e:
            if not _is_parse_error(e):
                raise
            await bot.send_message(
                chat_id=chat_id, text=text, business_connection_id=conn_id,
                parse_mode=None,
            )
        return True, None
    except TelegramForbiddenError as e:
        return False, f"forbidden: {e.message}"
    except TelegramBadRequest as e:
        # Most common here: no existing chat to initiate from.
        return False, f"bad_request: {e.message}"
    except Exception as e:  # noqa: BLE001
        logger.exception("deliver failed")
        return False, str(e)


def _is_parse_error(e: TelegramBadRequest) -> bool:
    """True when Telegram rejected the message because of bad HTML entities."""
    return "can't parse entities" in (e.message or "").lower()


def _customer_label(chat_id: int, username: str | None) -> str:
    """How a contact is shown on the draft card. With a @username the owner
    can tap straight through to the chat; otherwise fall back to the raw id."""
    if username:
        return f"@{username} (<code>{chat_id}</code>)"
    return f"<code>{chat_id}</code>"


def _preview(kind: str, chat_id: int, text: str, reason: str | None,
             *, escape_text: bool, username: str | None = None) -> str:
    head = "📨 Черновик ответа" if kind == "reply" else "📣 Сообщение"
    note = f"\n⚠️ {html.escape(reason)}" if reason else ""
    # Show the draft exactly as the contact would see it — render the model's
    # HTML. If it turns out to be malformed (parse error on send), we re-render
    # with the text escaped so the owner still sees the draft as raw text.
    body = html.escape(text) if escape_text else text
    return f"{head} → {_customer_label(chat_id, username)}{note}\n\n{body}"


async def send_or_approve(bot: Bot, owner_id: int, conn_id: str, chat_id: int,
                          kind: str, text: str, *, auto: bool,
                          reason: str | None = None,
                          username: str | None = None) -> bool:
    """auto=True → send immediately as the owner.
    auto=False → store a draft and DM the owner Send/Edit/Skip buttons.

    `username` (the contact's Telegram @handle, when known) is shown on the
    draft card so the owner can tap through to the chat."""
    if auto:
        ok, err = await deliver(bot, conn_id, chat_id, text)
        if ok:
            await local.add_message(chat_id, "assistant", text)
            return True
        # Couldn't reach as the owner — fall back to manual approval/notice.
        reason = (reason or "") + f" (не доставлено: {err})"
        auto = False

    draft_id = await local.create_draft(owner_id, conn_id, chat_id, kind, text)
    try:
        try:
            await bot.send_message(
                owner_id,
                _preview(kind, chat_id, text, reason, escape_text=False,
                         username=username),
                reply_markup=draft_kb(draft_id),
            )
        except TelegramBadRequest as e:
            if not _is_parse_error(e):
                raise
            # Model emitted broken HTML — show it escaped so the card still sends.
            await bot.send_message(
                owner_id,
                _preview(kind, chat_id, text, reason, escape_text=True,
                         username=username),
                reply_markup=draft_kb(draft_id),
            )
    except Exception:  # noqa: BLE001
        logger.exception("could not notify owner %s about draft", owner_id)
    return False
