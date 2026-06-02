from aiogram.fsm.state import State, StatesGroup


class OwnerStates(StatesGroup):
    setting_style = State()      # capturing a manual writing-style override
    editing_draft = State()      # editing a drafted reply before sending
    awaiting_export = State()    # waiting for the Telegram export to learn from
