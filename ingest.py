"""Telegram Desktop export parser + style profiler.

Standalone CLI **and** importable module. It reads a Telegram Desktop JSON
export, extracts the OWNER's own messages from their 1:1 personal chats,
filters out anything sensitive (emails, cards, passwords, links), stores the
samples in the local SQLite store, then builds a style guide + few-shot example
block the bot uses to reply AS the owner.

Usage:
    python ingest.py <path-to-result.json> [--owner-id N] [--owner-name NAME]
                     [--max-samples 4000] [--no-llm]

Robust to truncated/corrupt JSON: if a full parse fails it salvages as many
individual message objects as it can.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from typing import Optional

from bot.db import local
from bot.services import style

logger = logging.getLogger("ingest")

# ── text reconstruction ─────────────────────────────────────────────────────
def reconstruct_text(msg: dict) -> str:
    """Robustly flatten a Telegram message's ``text`` into plain text.

    Prefer the flat ``text_entities`` form when present; otherwise handle
    ``text`` as a string or a list of strings / ``{"type","text"}`` objects.
    """
    ents = msg.get("text_entities")
    if isinstance(ents, list) and ents:
        parts = []
        for e in ents:
            if isinstance(e, dict):
                parts.append(str(e.get("text", "")))
            elif isinstance(e, str):
                parts.append(e)
        joined = "".join(parts).strip()
        if joined:
            return joined

    text = msg.get("text")
    if isinstance(text, str):
        return text.strip()
    if isinstance(text, list):
        parts = []
        for item in text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
        return "".join(parts).strip()
    return ""


# ── privacy / quality filters ───────────────────────────────────────────────
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_DIGITS_RE = re.compile(r"\d{12,}")
_CARD_GROUPS_RE = re.compile(r"(?:\d[ -]?){13,}")  # 13+ digits w/ optional sep
_URL_RE = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+", re.IGNORECASE)
_URL_ONLY_RE = re.compile(r"^\s*(?:https?://\S+|www\.\S+|t\.me/\S+)\s*$", re.IGNORECASE)
_CREDENTIAL_WORDS = ("пароль", "password", "логин", "login")
_SENSITIVE_ENTITY_TYPES = {"email", "phone", "bank_card", "cashtag"}

MAX_TEXT_LEN = 600


def _entities_sensitive(msg: dict) -> bool:
    ents = msg.get("text_entities")
    if not isinstance(ents, list):
        return False
    for e in ents:
        if isinstance(e, dict) and e.get("type") in _SENSITIVE_ENTITY_TYPES:
            return True
    return False


def _looks_like_credentials(text: str) -> bool:
    low = text.lower()
    for w in _CREDENTIAL_WORDS:
        idx = low.find(w)
        if idx != -1:
            # a credential word followed by some token (value) → likely a dump
            tail = low[idx + len(w):].strip(" :=-\n\t")
            if tail:
                return True
    # multiple lines that look like user/pass pairs
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        hits = sum(
            1 for ln in lines
            if re.search(r"\b(user|email|pass|pwd|login)\b", ln, re.IGNORECASE)
        )
        if hits >= 2:
            return True
    return False


def is_private_or_unsafe(text: str, msg: dict) -> bool:
    """True if the message should be DROPPED for privacy/quality reasons."""
    if _entities_sensitive(msg):
        return True
    if _EMAIL_RE.search(text):
        return True
    if _LONG_DIGITS_RE.search(text) or _CARD_GROUPS_RE.search(text):
        return True
    if _looks_like_credentials(text):
        return True
    if _URL_ONLY_RE.match(text):
        return True
    return False


def is_owner_message(msg: dict, owner_from_id: str) -> bool:
    return msg.get("from_id") == owner_from_id


def passes_message_filters(msg: dict, text: str) -> bool:
    """Per-message quality/privacy gate (assumes already confirmed from owner).

    Returns True if the message is a usable style sample.
    """
    if msg.get("type") != "message":
        return False
    if msg.get("forwarded_from") or msg.get("forwarded_from_id"):
        return False
    if not text:
        return False
    if len(text) > MAX_TEXT_LEN:
        return False
    if is_private_or_unsafe(text, msg):
        return False
    return True


# ── owner id resolution ──────────────────────────────────────────────────────
def resolve_owner_id(data: Optional[dict], cli_owner_id: Optional[int],
                     salvaged_user_id: Optional[int]) -> int:
    if cli_owner_id is not None:
        return cli_owner_id
    if data is not None:
        pi = data.get("personal_information")
        if isinstance(pi, dict) and pi.get("user_id") is not None:
            return int(pi["user_id"])
    if salvaged_user_id is not None:
        return int(salvaged_user_id)
    raise ValueError(
        "Could not determine owner id: single-chat export has no "
        "personal_information; pass --owner-id <your telegram user id>."
    )


# ── sample extraction (clean mode) ───────────────────────────────────────────
def _walk_chat_messages(messages: list, owner_from_id: str) -> list[dict]:
    """Walk one chat's messages IN ORDER, emitting owner reply samples.

    Tracks the last non-owner message text as the 'incoming'. A burst of
    consecutive owner replies all map to the same preceding incoming.
    """
    samples: list[dict] = []
    last_incoming: Optional[str] = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = reconstruct_text(msg)
        if is_owner_message(msg, owner_from_id):
            if passes_message_filters(msg, text):
                samples.append({
                    "incoming": last_incoming,
                    "reply": text,
                    "length": len(text),
                    "has_emoji": style.has_emoji(text),
                })
            # do NOT reset last_incoming on consecutive owner messages
        else:
            # a non-owner message: update the incoming context if it's usable text
            if msg.get("type") == "message" and text:
                last_incoming = text
    return samples


def extract_clean(data: dict, owner_from_id: str) -> tuple[list[dict], int]:
    """Extract samples from a full DataExport, 1:1 personal chats only.

    Returns ``(samples, chats_scanned)``.
    """
    samples: list[dict] = []
    chats_scanned = 0
    chats = (data.get("chats") or {}).get("list")
    if isinstance(chats, list):
        for chat in chats:
            if not isinstance(chat, dict):
                continue
            if chat.get("type") != "personal_chat":
                continue
            chats_scanned += 1
            msgs = chat.get("messages")
            if isinstance(msgs, list):
                samples.extend(_walk_chat_messages(msgs, owner_from_id))
    elif isinstance(data.get("messages"), list):
        # Single-chat export that happened to parse cleanly.
        if data.get("type") == "personal_chat" or data.get("type") is None:
            chats_scanned = 1
            samples.extend(_walk_chat_messages(data["messages"], owner_from_id))
    return samples, chats_scanned


# ── salvage mode (truncated/corrupt JSON) ────────────────────────────────────
_MSG_START_RE = re.compile(r'\{\s*"id"\s*:')
_USER_ID_RE = re.compile(r'"personal_information"\s*:\s*\{[^}]*?"user_id"\s*:\s*(\d+)')


def salvage_messages(raw: str) -> list[dict]:
    """Scan raw text and decode as many individual message objects as possible.

    Uses ``json.JSONDecoder().raw_decode`` starting at each ``{"id":`` match.
    """
    decoder = json.JSONDecoder()
    out: list[dict] = []
    for m in _MSG_START_RE.finditer(raw):
        start = m.start()
        try:
            obj, _ = decoder.raw_decode(raw, start)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and ("from_id" in obj or "actor_id" in obj or "text" in obj):
            out.append(obj)
    return out


def salvage_user_id(raw: str) -> Optional[int]:
    m = _USER_ID_RE.search(raw)
    return int(m.group(1)) if m else None


def extract_salvage(messages: list[dict], owner_from_id: str) -> list[dict]:
    """Salvage mode: chat boundaries/types are unknown, so we can't restrict to
    1:1 chats. Treat the whole salvaged stream as one ordered sequence and apply
    the per-message filters. Logs a warning that chat-type filtering is gone.
    """
    logger.warning(
        "Salvage mode: chat-type (1:1-only) filtering unavailable — applying "
        "per-message filters across all salvaged messages."
    )
    return _walk_chat_messages(messages, owner_from_id)


# ── orchestration ────────────────────────────────────────────────────────────
def parse_export(path: str, cli_owner_id: Optional[int]) -> tuple[list[dict], int, int, bool]:
    """Parse the export at ``path``.

    Returns ``(samples, owner_id, chats_scanned, salvaged)``.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Full JSON parse failed (%s) — falling back to salvage.", e)
        salvaged_msgs = salvage_messages(raw)
        logger.info("Salvaged %d message objects.", len(salvaged_msgs))
        owner_id = resolve_owner_id(None, cli_owner_id, salvage_user_id(raw))
        owner_from_id = f"user{owner_id}"
        samples = extract_salvage(salvaged_msgs, owner_from_id)
        return samples, owner_id, 0, True

    owner_id = resolve_owner_id(data, cli_owner_id, None)
    owner_from_id = f"user{owner_id}"
    samples, chats_scanned = extract_clean(data, owner_from_id)
    return samples, owner_id, chats_scanned, False


async def run_ingest(path, owner_id: Optional[int] = None,
                     owner_name: str = "me", *, max_samples: int = 4000,
                     use_llm: bool = True) -> dict:
    """Async entry point usable from the bot (/learn) and the CLI.

    ``path`` is a single export path or a list of them — samples from every file
    are accumulated (the owner's style across all of them) before the store is
    cleared once and the profile rebuilt. Parses, stores samples, builds + stores
    the style profile, and returns a summary dict.
    """
    from config import settings

    paths = [path] if isinstance(path, str) else list(path)

    samples: list[dict] = []
    chats_scanned = 0
    salvaged = False
    resolved_owner_id: Optional[int] = owner_id
    files_read = 0
    for p in paths:
        # Once we know the owner id (from the first file's personal_information),
        # reuse it for the rest — later single-chat exports have no such block.
        s, oid, scanned, sal = parse_export(p, resolved_owner_id)
        if resolved_owner_id is None:
            resolved_owner_id = oid
        samples.extend(s)
        chats_scanned += scanned
        salvaged = salvaged or sal
        files_read += 1
        logger.info("%s → %d samples (owner %s%s)", p, len(s), oid,
                    ", salvaged" if sal else "")

    # Cap: stride evenly across the whole merged set rather than taking the tail,
    # so every chat and time period stays represented (a plain tail-slice would,
    # when several files are merged, keep only the last file's messages).
    capped = False
    if max_samples and len(samples) > max_samples:
        step = len(samples) / max_samples
        samples = [samples[int(i * step)] for i in range(max_samples)]
        capped = True

    num_examples = settings.style_num_examples

    await local.init()
    try:
        await local.clear_style_samples(resolved_owner_id)
        for s in samples:
            await local.add_style_sample(
                resolved_owner_id, s["incoming"], s["reply"],
                s["length"], s["has_emoji"],
            )

        guide, examples_text = await style.build_style_profile(
            samples, owner_name, num_examples=num_examples, use_llm=use_llm,
        )
        llm_guide = use_llm and bool(samples) and not guide.startswith(
            f"{owner_name}'s texting style (auto-summarized"
        )
        examples_selected = examples_text.count("\n—") + (
            1 if examples_text.startswith("—") else 0
        )
        await local.set_style_profile(resolved_owner_id, guide, examples_text)
    finally:
        await local.close()

    summary = {
        "owner_id": resolved_owner_id,
        "files_read": files_read,
        "chats_scanned": chats_scanned,
        "samples_kept": len(samples),
        "examples_selected": examples_selected,
        "llm_guide": llm_guide,
        "salvaged": salvaged,
        "capped": capped,
    }
    return summary


def _format_summary(s: dict) -> str:
    return (
        "Style learning complete.\n"
        f"  Mode:               {'salvage (truncated JSON)' if s['salvaged'] else 'clean'}\n"
        f"  Files read:         {s.get('files_read', 1)}\n"
        f"  Owner id:           {s['owner_id']}\n"
        f"  1:1 chats scanned:  {s['chats_scanned']}\n"
        f"  Samples kept:       {s['samples_kept']}"
        f"{' (capped)' if s['capped'] else ''}\n"
        f"  Examples selected:  {s['examples_selected']}\n"
        f"  Style guide:        {'LLM-generated' if s['llm_guide'] else 'heuristic (offline)'}"
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    ap = argparse.ArgumentParser(description="Ingest a Telegram export and learn the owner's style.")
    ap.add_argument("path", nargs="+",
                    help="One or more result.json exports (samples are merged)")
    ap.add_argument("--owner-id", type=int, default=None,
                    help="Your Telegram user id (required for single-chat exports)")
    ap.add_argument("--owner-name", default=None,
                    help="Your name/handle for the style profile (defaults to OWNER_NAME)")
    ap.add_argument("--max-samples", type=int, default=4000,
                    help="Cap on stored samples (keeps the most recent)")
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip the LLM and use a heuristic style guide (offline)")
    args = ap.parse_args()

    owner_name = args.owner_name
    if owner_name is None:
        try:
            from config import settings
            owner_name = settings.owner_name
        except Exception:  # noqa: BLE001 — config may be incomplete when offline
            owner_name = "me"

    summary = asyncio.run(run_ingest(
        args.path, owner_id=args.owner_id, owner_name=owner_name,
        max_samples=args.max_samples, use_llm=not args.no_llm,
    ))
    print(_format_summary(summary))


if __name__ == "__main__":
    main()
