"""Commands for the owner to configure and control the assistant."""

from __future__ import annotations

import logging
import os

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from bot.db import local
from bot.services.dispatch import deliver
from bot.utils.prompts import DEFAULT_STYLE
from bot.utils.states import OwnerStates

import ingest

logger = logging.getLogger(__name__)
router = Router(name="owner")
router.message.filter(F.from_user.id.in_(settings.owner_ids))

# Telegram Bot API caps bot file downloads at ~20 MB.
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

DATA_DIR = os.path.dirname(settings.local_db_path) or "."


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Я — ваш личный ассистент в Telegram.\n\n"
        "Я отвечаю в ваших <b>личных чатах от вашего имени</b>, подражая вашему "
        "стилю переписки.\n\n"
        "<b>Как подключить:</b>\n"
        "1. Настройки Telegram → <b>Telegram для бизнеса</b> → <b>Чат-боты</b>.\n"
        "2. Добавьте этого бота и выберите чаты.\n"
        "3. Включите право «Отвечать на сообщения».\n\n"
        "<b>Как научить меня вашему стилю:</b>\n"
        "Выгрузите чаты в Telegram Desktop (Settings → Advanced → Export Telegram "
        "data, формат JSON) и пришлите мне <code>result.json</code> командой /learn. "
        "Большие выгрузки удобнее обработать на сервере: "
        "<code>python ingest.py result.json --owner-id &lt;ваш id&gt;</code>.\n\n"
        "Команды: /status · /style · /learn · /auto · /samples"
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    owner_id = message.from_user.id
    conn = await local.get_owner_connection(owner_id)
    owner = await local.get_owner(owner_id)
    if owner and owner["auto_reply"] is not None:
        auto = bool(owner["auto_reply"])
    else:
        auto = settings.auto_reply
    style_set = bool(owner and owner["style_prompt"])
    n_samples = await local.count_style_samples(owner_id)
    lines = [
        f"🔌 Подключение: {'✅ активно' if conn else '❌ нет'}",
        f"🤖 Авто-ответы: {'вкл' if auto else 'выкл (черновики на подтверждение)'}",
        f"✍️ Стиль: {'задан' if style_set else 'по умолчанию'}",
        f"📚 Выучено примеров: {n_samples}",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("style"))
async def cmd_style(message: Message, state: FSMContext) -> None:
    await state.set_state(OwnerStates.setting_style)
    await message.answer(
        "✍️ Опишите, как вы пишете — тон, длина, эмодзи и т.д. Это переопределит "
        "выученный стиль.\n\n"
        f"<i>Пример:</i> {DEFAULT_STYLE}\n\n"
        "Отправьте текст одним сообщением (или /cancel)."
    )


@router.message(OwnerStates.setting_style, Command("cancel"))
async def cancel_style(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(OwnerStates.setting_style, F.text)
async def save_style(message: Message, state: FSMContext) -> None:
    await local.set_style_prompt(message.from_user.id, message.text.strip())
    await state.clear()
    await message.answer("✅ Стиль сохранён.")


@router.message(Command("auto"))
async def cmd_auto(message: Message) -> None:
    arg = (message.text or "").split(maxsplit=1)
    val = arg[1].strip().lower() if len(arg) > 1 else ""
    if val in ("on", "вкл", "1", "true"):
        await local.set_auto_reply(message.from_user.id, True)
        await message.answer("🤖 Авто-ответы включены.")
    elif val in ("off", "выкл", "0", "false"):
        await local.set_auto_reply(message.from_user.id, False)
        await message.answer("✍️ Авто-ответы выключены — буду присылать черновики.")
    else:
        await message.answer("Использование: <code>/auto on</code> или <code>/auto off</code>")


@router.message(Command("samples"))
async def cmd_samples(message: Message) -> None:
    n = await local.count_style_samples(message.from_user.id)
    await message.answer(f"📚 Выучено примеров вашего стиля: {n}")


@router.message(Command("learn"))
async def cmd_learn(message: Message, state: FSMContext) -> None:
    await state.set_state(OwnerStates.awaiting_export)
    await message.answer(
        "📥 Пришлите вашу выгрузку Telegram (<code>result.json</code>) как "
        "<b>файл/документ</b>.\n\n"
        "В Telegram Desktop: Settings → Advanced → Export Telegram data → формат "
        "<b>JSON</b>. Я возьму только ваши сообщения из личных 1:1 чатов и удалю "
        "из них всё чувствительное (почты, карты, пароли).\n\n"
        "⚠️ Выгрузки больше ~20 МБ Telegram не даёт скачать боту — для них "
        "запустите на сервере:\n"
        "<code>python ingest.py result.json --owner-id "
        f"{message.from_user.id} --owner-name {settings.owner_name}</code>\n\n"
        "Отмена: /cancel"
    )


@router.message(OwnerStates.awaiting_export, Command("cancel"))
async def cancel_learn(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(OwnerStates.awaiting_export, F.document)
async def on_export(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    if doc.file_size and doc.file_size > _MAX_DOWNLOAD_BYTES:
        await state.clear()
        await message.answer(
            "⚠️ Файл больше 20 МБ — Telegram не даёт ботам скачивать такие. "
            "Запустите обработку на сервере:\n"
            "<code>python ingest.py result.json --owner-id "
            f"{message.from_user.id} --owner-name {settings.owner_name}</code>"
        )
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    name = doc.file_name or f"export_{message.from_user.id}.json"
    path = os.path.join(DATA_DIR, name)
    await message.answer("⏳ Скачиваю и учусь вашему стилю — это может занять минуту…")
    try:
        await bot.download(doc, destination=path)
    except Exception as e:  # noqa: BLE001
        await state.clear()
        logger.warning("export download failed: %s", e)
        await message.answer(
            "❌ Не удалось скачать файл (возможно, он слишком большой). "
            "Запустите на сервере:\n"
            "<code>python ingest.py result.json --owner-id "
            f"{message.from_user.id} --owner-name {settings.owner_name}</code>"
        )
        return

    try:
        summary = await ingest.run_ingest(
            path, owner_id=message.from_user.id, owner_name=settings.owner_name,
        )
    except Exception as e:  # noqa: BLE001
        await state.clear()
        logger.exception("ingest failed")
        await message.answer(f"❌ Не удалось обработать выгрузку: {e}")
        return

    await state.clear()
    await message.answer(
        "✅ Готово!\n"
        f"Чатов 1:1 просмотрено: {summary['chats_scanned']}\n"
        f"Сообщений выучено: {summary['samples_kept']}\n"
        f"Примеров для подсказок: {summary['examples_selected']}\n"
        f"Гайд по стилю: {'сгенерирован ИИ' if summary['llm_guide'] else 'эвристический'}"
        + ("\n⚠️ Файл был обрезан — использован режим спасения данных." if summary["salvaged"] else "")
    )


# ── draft approval callbacks (not owner-filtered above, so filter here) ─────
@router.callback_query(F.data.startswith("d:"))
async def on_draft_action(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    if call.from_user.id not in settings.owner_ids:
        await call.answer()
        return
    _, action, draft_id_s = call.data.split(":")
    draft_id = int(draft_id_s)
    draft = await local.get_draft(draft_id)
    if not draft:
        await call.answer("Черновик не найден", show_alert=True)
        return

    if action == "skip":
        await local.delete_draft(draft_id)
        await call.message.edit_text(call.message.html_text + "\n\n❌ Пропущено")
        await call.answer("Пропущено")
        return

    if action == "edit":
        await state.set_state(OwnerStates.editing_draft)
        await state.update_data(draft_id=draft_id)
        await call.message.answer("✏️ Пришлите новый текст сообщения:")
        await call.answer()
        return

    if action == "send":
        ok, err = await deliver(bot, draft["conn_id"], draft["chat_id"], draft["text"])
        if ok:
            await local.add_message(draft["chat_id"], "assistant", draft["text"])
            await local.delete_draft(draft_id)
            await call.message.edit_text(call.message.html_text + "\n\n✅ Отправлено")
            await call.answer("Отправлено")
        else:
            await call.answer(f"Не удалось: {err}", show_alert=True)


@router.message(OwnerStates.editing_draft, F.text)
async def on_edit_draft(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    draft_id = data.get("draft_id")
    draft = await local.get_draft(draft_id) if draft_id else None
    if not draft:
        await state.clear()
        await message.answer("Черновик не найден.")
        return
    ok, err = await deliver(bot, draft["conn_id"], draft["chat_id"], message.text)
    await state.clear()
    if ok:
        await local.add_message(draft["chat_id"], "assistant", message.text)
        await local.delete_draft(draft_id)
        await message.answer("✅ Отправлено.")
    else:
        await message.answer(f"❌ Не удалось отправить: {err}")
