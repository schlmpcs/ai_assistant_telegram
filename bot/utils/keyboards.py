from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def draft_kb(draft_id: int) -> InlineKeyboardMarkup:
    """Approve / edit / skip a drafted message before it goes out as the owner."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data=f"d:send:{draft_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"d:edit:{draft_id}"),
        InlineKeyboardButton(text="❌ Пропустить", callback_data=f"d:skip:{draft_id}"),
    ]])
