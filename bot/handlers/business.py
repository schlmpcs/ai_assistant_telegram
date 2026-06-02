"""Telegram Business / Chat Automation handlers.

You link this bot to your personal account (Settings → Telegram Business →
Chat-bots, enable "Reply to messages"). Incoming private messages then arrive
here as `business_message` updates; replies go out AS you, in your learned
style, via `business_connection_id`.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, Message

from config import settings
from bot.db import local
from bot.services import llm
from bot.services.dispatch import send_or_approve
from bot.utils import prompts

logger = logging.getLogger(__name__)
router = Router(name="business")

# Per-contact debounce. People often split one thought across several quick
# messages; rather than answer each line, we hold a short window and let the
# latest message's task generate one reply for the whole burst. A newer message
# cancels the previous pending task and accumulates its text.
_pending: dict[int, asyncio.Task] = {}
_pending_texts: dict[int, list[str]] = {}


def _customer_ref(customer_id: int, username: str | None) -> str:
    """How a contact is named in owner pings. With a @username the owner can tap
    straight through to the chat; otherwise fall back to the raw id. Mirrors
    `dispatch._customer_label`."""
    if username:
        return f"@{username} (<code>{customer_id}</code>)"
    return f"<code>{customer_id}</code>"


@router.business_connection()
async def on_connection(event: BusinessConnection, bot: Bot) -> None:
    """Owner connected (or toggled) the bot on their account."""
    # aiogram moved this field: <3.15 exposes `can_reply`, newer uses `rights.can_reply`.
    rights = getattr(event, "rights", None)
    if rights is not None:
        can_reply = bool(getattr(rights, "can_reply", True))
    else:
        can_reply = bool(getattr(event, "can_reply", True))
    await local.upsert_connection(
        conn_id=event.id,
        owner_id=event.user.id,
        is_enabled=event.is_enabled,
        can_reply=bool(can_reply),
    )
    if event.is_enabled and can_reply:
        msg = ("✅ Бот подключён к вашему аккаунту. Теперь я буду отвечать в ваших "
               "личных чатах <b>от вашего имени</b>, в вашем стиле.\n\n"
               "Сначала научите меня вашему стилю: /learn.\n"
               "Команды: /status · /style · /learn · /auto")
    elif event.is_enabled and not can_reply:
        msg = ("⚠️ Бот подключён, но без права отвечать. Включите «Отвечать на "
               "сообщения» в настройках подключения, чтобы я мог писать за вас.")
    else:
        msg = "🔌 Бот отключён от аккаунта."
    try:
        await bot.send_message(event.user.id, msg)
    except Exception:  # noqa: BLE001
        logger.warning("could not DM owner %s on connection", event.user.id)


@router.business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    conn_id = message.business_connection_id
    if not conn_id or not message.text:
        return

    conn = await local.get_connection(conn_id)
    if not conn or not conn["is_enabled"] or not conn["can_reply"]:
        return

    owner_id = conn["owner_id"]

    # The owner's own outgoing messages also arrive here — record for context,
    # don't reply. A manual message also means the owner has taken this chat
    # over: start (or reset) a quiet window so the bot doesn't talk over them.
    if message.from_user and message.from_user.id == owner_id:
        await local.add_message(message.chat.id, "assistant", message.text)
        # But not every owner message is them stepping in by hand: Telegram
        # Business away/greeting auto-replies arrive with `is_from_offline`, and
        # messages this bot itself sent on their behalf carry `sender_business_bot`.
        # Treating those as a takeover would let one automatic greeting silence
        # the bot for `manager_takeover_hours`. Only a genuinely hand-typed
        # message opens the quiet window.
        is_automated = bool(message.is_from_offline) or message.sender_business_bot is not None
        if not is_automated:
            await local.record_manager_activity(message.chat.id)
        return

    customer_id = message.chat.id
    await local.add_message(customer_id, "user", message.text)

    # If the owner recently replied to this contact by hand, stay out of the
    # chat — keep storing messages for context, but let the owner handle it.
    if await local.manager_active_within(customer_id, settings.manager_takeover_hours):
        logger.info(
            "Owner active in chat %s within %dh — skipping bot reply",
            customer_id, settings.manager_takeover_hours,
        )
        return

    # Abuse guard: cap paid LLM calls per contact. The message is still stored
    # (kept for context), we just don't generate a reply while they're flooding.
    now = int(time.time())
    per_min, per_hour = await local.count_recent_user_messages(
        customer_id, now - 60, now - 3600
    )
    if per_min > settings.reply_rate_per_min or per_hour > settings.reply_rate_per_hour:
        logger.info(
            "Rate-limited contact %s (%d/min, %d/hr) — skipping LLM reply",
            customer_id, per_min, per_hour,
        )
        return

    # Hold a short window for follow-up messages, then reply to the whole burst
    # at once. The newest message's task supersedes any earlier pending one.
    _pending_texts.setdefault(customer_id, []).append(message.text)
    pending = _pending.get(customer_id)
    if pending and not pending.done():
        pending.cancel()
    username = message.from_user.username if message.from_user else None
    _pending[customer_id] = asyncio.create_task(
        _debounced_reply(bot, conn_id, owner_id, customer_id, username)
    )


async def _debounced_reply(
    bot: Bot, conn_id: str, owner_id: int, customer_id: int, username: str | None
) -> None:
    """After a quiet window, generate one reply for the contact's message burst."""
    try:
        await asyncio.sleep(settings.reply_debounce_seconds)
    except asyncio.CancelledError:
        return  # a newer message arrived — its task will answer the full batch
    # Past the quiet window: claim the batch. We no longer cancel ourselves, so a
    # message arriving mid-generation starts a fresh cycle rather than aborting.
    _pending.pop(customer_id, None)
    texts = _pending_texts.pop(customer_id, [])
    customer_text = "\n".join(texts).strip()

    owner = await local.get_owner(owner_id)
    style = owner["style_prompt"] if owner else None
    examples = owner["style_examples"] if owner else None
    if owner and owner["auto_reply"] is not None:
        auto = bool(owner["auto_reply"])
    else:
        auto = settings.auto_reply

    escalate = prompts.needs_escalation(customer_text)

    history = await local.get_history(customer_id, limit=10)
    llm_messages = [{
        "role": "system",
        "content": prompts.system_prompt(style, examples, settings.owner_name),
    }]
    llm_messages += history  # already ends with the contact's latest messages

    reply = await llm.chat(llm_messages)
    if not reply:
        # Don't auto-send anything if the model failed — ping the owner.
        try:
            await bot.send_message(
                owner_id,
                f"🤖 {_customer_ref(customer_id, username)} написал вам, но ИИ не "
                f"составил ответ — ответьте сами.\n\n«{html.escape(customer_text)}»",
            )
        except Exception:  # noqa: BLE001
            pass
        return

    # The model flags replies it can't make for real (needs a private fact or a
    # commitment). We still send the contact a natural holding line, but always
    # ping the owner to follow up — the bot must not be the last word here.
    wants_manager, reply = prompts.split_call_manager(reply)

    if wants_manager:
        reason = "нужен твой ответ"
    elif escalate:
        reason = "деликатная тема — проверь"
    else:
        reason = None

    # When the bot is only buying time (a holding line), it's safe to send the
    # contact immediately even in draft mode — it makes no claims and the owner
    # is pinged separately below to give the real answer. Sensitive topics still
    # always draft for approval.
    auto_send = (auto or wants_manager) and not escalate

    await send_or_approve(
        bot, owner_id, conn_id, customer_id, "reply", reply,
        auto=auto_send,
        reason=reason,
        username=username,
    )

    if wants_manager:
        try:
            await bot.send_message(
                owner_id,
                f"🙋 {_customer_ref(customer_id, username)} написал что-то, на что я "
                f"не могу ответить за вас — нужен ваш ответ.\n\n"
                f"«{html.escape(customer_text)}»",
            )
        except Exception:  # noqa: BLE001
            pass
