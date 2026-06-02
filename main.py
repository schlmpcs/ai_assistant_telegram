"""Entry point: init the store, build the bot, register routers, start polling.

This bot receives Telegram Business updates (business_connection /
business_message) and replies in the owner's private chats AS the owner, in
their learned texting style. There is no scheduler, no payments, no outreach.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from bot.db import local
from bot.handlers import business, owner
from bot.utils.prompts import DEFAULT_STYLE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("assistant_bot")


async def main() -> None:
    await local.init()

    # Seed a neutral default voice as the active per-owner style on first run
    # (non-destructive: a style learned via /learn or set via /style is kept).
    for owner_id in settings.owner_ids:
        await local.seed_style_prompt(owner_id, DEFAULT_STYLE)

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(owner.router)       # owner commands + draft approvals
    dp.include_router(business.router)    # business connection + messages

    me = await bot.get_me()
    logger.info("Started as @%s", me.username)
    logger.info(
        "LLM: %s via %s", ", ".join(settings.llm_models), settings.llm_base_url
    )
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(
            bot, allowed_updates=dp.resolve_used_update_types()
        )
    finally:
        await local.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped")
