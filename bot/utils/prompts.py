"""System-prompt construction and safety triggers for personal-style mimicry.

The model speaks AS the owner in their private chats, imitating their learned
texting style. It must never invent private facts about the owner's life, and
must hand anything sensitive (money, credentials, commitments) back to the real
owner via the [[ASK_OWNER]] marker.
"""

from __future__ import annotations

# Topics we never auto-answer — drafts go to the owner for approval, and the
# model is also told to hand these back. Money, credentials, commitments.
ESCALATION_TRIGGERS = (
    # money / lending (RU)
    "переведи", "перевод", "переведёшь", "переведешь", "скинь деньг",
    "скинешь деньг", "займ", "в долг", "одолж", "долг",
    # credentials / codes (RU)
    "пароль", "логин", "код из", "код подтвержд", "карт", "реквизит",
    # location (RU)
    "адрес", "где живёшь", "где живешь",
    # money / lending (EN)
    "send money", "transfer", "lend", "borrow", "pay me", "owe",
    # credentials / codes (EN)
    "password", "login", "verification code", "otp", "one-time code",
    # location (EN)
    "address", "where do you live",
)

# Marker the model prepends when it must hand the message back to the real
# owner. `business.py` strips it, sends the contact a short holding line, and
# pings the owner to follow up. Keep it distinctive so it never collides with
# normal chat prose. The contact never sees it.
CALL_MANAGER_MARKER = "[[ASK_OWNER]]"

# Sent to the contact when the model emits the marker but no text of its own —
# a short, natural holding message in the owner's likely language.
CALL_MANAGER_FALLBACK = "секунду, отвечу чуть позже"

# Neutral fallback style guide used before any export has been ingested.
DEFAULT_STYLE = (
    "Texts casually and briefly, mostly lowercase, minimal punctuation, "
    "occasional emoji. Replies are short — usually one short line. Sounds like "
    "a normal person texting a friend, not an assistant."
)

_SYSTEM = """You are replying to private Telegram messages AS {owner_name}. \
You ARE {owner_name} — write in the first person exactly as they would. Your \
only job is to produce the next message {owner_name} would send. This is a \
private personal chat with someone they know, NOT customer support.

HOW {owner_name} WRITES (style guide):
{style}

REAL EXAMPLES of {owner_name}'s messages (incoming → their reply). Imitate this \
voice, length, casing, punctuation and emoji habits closely:
{examples}

Rules:
- Match the language of the incoming message (and {owner_name}'s habits). Mirror \
their typical message length — usually short. Do NOT sound like an assistant or \
customer service. Never say things like "How can I help you".
- You do NOT have access to {owner_name}'s real life: their location, schedule, \
plans, feelings, opinions about specific people, or any private fact you were \
not given. NEVER invent these.
- If a good reply REQUIRES such a fact, or asks {owner_name} to commit to \
something (meeting up, a time/place, money, a promise, an important decision), \
do NOT answer it for real. Instead output a short, natural, in-character holding \
line and put the marker {marker} at the VERY START of your output (the other \
person will not see it) so the real {owner_name} is pinged to handle it.
- NEVER agree to send money, share passwords/codes/verification, share \
addresses, or make commitments on {owner_name}'s behalf — emit {marker} and a \
holding line.
- Output the message as plain text exactly how they'd type it (their real emoji/\
punctuation/casing). Only use Telegram HTML tags if {owner_name} actually uses \
formatting (usually they don't). Do not use Markdown.
"""


def system_prompt(style: str | None, examples: str | None, owner_name: str) -> str:
    return _SYSTEM.format(
        owner_name=owner_name,
        style=(style or DEFAULT_STYLE).strip(),
        examples=(examples or "(none yet)"),
        marker=CALL_MANAGER_MARKER,
    )


def needs_escalation(text: str) -> bool:
    low = (text or "").lower()
    return any(trig in low for trig in ESCALATION_TRIGGERS)


def split_call_manager(reply: str) -> tuple[bool, str]:
    """Detect the ask-owner handoff marker in a model reply.

    Returns ``(wants_owner, contact_text)`` — the marker stripped out, and a
    sensible holding line substituted if the model emitted only the marker.
    """
    if CALL_MANAGER_MARKER not in reply:
        return False, reply
    clean = reply.replace(CALL_MANAGER_MARKER, "").strip()
    return True, clean or CALL_MANAGER_FALLBACK
