"""Build a style profile from ingested message samples.

Two outputs feed the system prompt:
  • a STYLE GUIDE  — prose describing how the owner texts (LLM-written, with a
    heuristic offline fallback), and
  • a FEW-SHOT EXAMPLE BLOCK — a deterministic, diverse selection of real
    message pairs the model imitates.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from bot.services import llm

# Common emoji unicode blocks (a pragmatic, not exhaustive, set).
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0000203C\U00002049]"
)

# Words ignored when computing the "frequent words" stat for the heuristic guide.
_STOPWORDS = {
    "и", "в", "не", "на", "что", "а", "то", "я", "ты", "это", "как", "с",
    "по", "за", "же", "так", "вот", "да", "нет", "ну", "у", "о", "из", "к",
    "the", "a", "to", "i", "you", "is", "it", "of", "and", "in", "for", "on",
}


def has_emoji(text: str) -> bool:
    return bool(_EMOJI_RE.search(text or ""))


def _len_bucket(length: int) -> int:
    """Coarse length quartile-ish bucket for diversity selection."""
    if length <= 12:
        return 0
    if length <= 40:
        return 1
    if length <= 120:
        return 2
    return 3


def select_examples(samples: list[dict], n: int) -> list[dict]:
    """Pick up to ``n`` diverse samples deterministically.

    Diversity = mix of length buckets and emoji flag, preferring pairs that
    HAVE an `incoming` (real reply pairs) but allowing some standalone ones.
    Round-robins across (length_bucket, has_emoji) buckets; within each bucket
    pairs-with-incoming come first. No randomness.
    """
    if n <= 0 or not samples:
        return []

    buckets: dict[tuple, list[dict]] = {}
    for s in samples:
        key = (_len_bucket(s["length"]), bool(s["has_emoji"]))
        buckets.setdefault(key, []).append(s)

    # Within each bucket, prefer samples that have an incoming message.
    for key in buckets:
        buckets[key].sort(key=lambda s: (s.get("incoming") is None,))

    ordered_keys = sorted(buckets.keys())
    selected: list[dict] = []
    idx = {k: 0 for k in ordered_keys}
    # Round-robin across buckets until we have n or all buckets exhausted.
    while len(selected) < n:
        progressed = False
        for k in ordered_keys:
            i = idx[k]
            if i < len(buckets[k]):
                selected.append(buckets[k][i])
                idx[k] += 1
                progressed = True
                if len(selected) >= n:
                    break
        if not progressed:
            break
    return selected


def format_examples(samples: list[dict], owner_name: str) -> str:
    """Render selected samples as a prompt-injectable block.

    Each pair:
        — «<incoming or (no preceding message)>»
        <owner_name>: «<reply>»
    """
    lines: list[str] = []
    for s in samples:
        incoming = (s.get("incoming") or "").strip() or "(no preceding message)"
        reply = (s.get("reply") or "").strip()
        lines.append(f"— «{incoming}»")
        lines.append(f"{owner_name}: «{reply}»")
        lines.append("")
    return "\n".join(lines).strip()


def heuristic_guide(samples: list[dict], owner_name: str) -> str:
    """Offline style guide derived from simple corpus statistics."""
    if not samples:
        return (
            f"{owner_name} texts casually and briefly; lowercase, minimal "
            "punctuation, occasional emoji."
        )
    n = len(samples)
    avg_len = sum(s["length"] for s in samples) / n
    pct_emoji = 100.0 * sum(1 for s in samples if s["has_emoji"]) / n

    words: Counter = Counter()
    lower_chars = upper_chars = 0
    q_marks = excl = 0
    for s in samples:
        t = s["reply"] or ""
        for ch in t:
            if ch.islower():
                lower_chars += 1
            elif ch.isupper():
                upper_chars += 1
        q_marks += t.count("?")
        excl += t.count("!")
        for w in re.findall(r"[\w']+", t.lower()):
            if len(w) >= 2 and w not in _STOPWORDS and not w.isdigit():
                words[w] += 1
    cased = lower_chars + upper_chars
    pct_lower = 100.0 * lower_chars / cased if cased else 0.0
    top = ", ".join(w for w, _ in words.most_common(12))

    return (
        f"{owner_name}'s texting style (auto-summarized from {n} messages):\n"
        f"- Typical message length: ~{avg_len:.0f} characters (mostly short).\n"
        f"- Emoji: about {pct_emoji:.0f}% of messages contain an emoji.\n"
        f"- Casing: ~{pct_lower:.0f}% of letters are lowercase "
        f"({'mostly lowercase' if pct_lower > 80 else 'mixed casing'}).\n"
        f"- Punctuation: {q_marks} question marks and {excl} exclamation marks "
        f"across the sample (light punctuation).\n"
        f"- Frequent words/phrases: {top}.\n"
        "Imitate this: keep it short, match the casing, and reuse the kind of "
        "words above."
    )


async def build_style_profile(
    samples: list[dict], owner_name: str, *, num_examples: int = 25,
    use_llm: bool = True,
) -> tuple[str, str]:
    """Return ``(guide, examples_text)``.

    The guide is written by the LLM from a larger diverse sample; on
    ``use_llm=False`` or LLM failure it falls back to a heuristic guide so
    ingestion always works offline.
    """
    examples = select_examples(samples, num_examples)
    examples_text = format_examples(examples, owner_name)

    guide: Optional[str] = None
    if use_llm and samples:
        # A larger diverse set for the guide: reply text, plus a few pairs.
        guide_samples = select_examples(samples, 120) or samples[:120]
        reply_lines = [f"- {s['reply']}" for s in guide_samples]
        pair_samples = [s for s in guide_samples if s.get("incoming")][:15]
        pair_lines = [
            f"- они написали: «{s['incoming']}» → ответ: «{s['reply']}»"
            for s in pair_samples
        ]
        corpus = "\n".join(reply_lines)
        pairs = "\n".join(pair_lines)
        sys = (
            "You are an expert at characterizing how a specific person writes "
            "text messages, for the purpose of perfectly imitating them."
        )
        user = (
            "Here are real text messages written by one person in their private "
            "chats. Write a concise but specific STYLE GUIDE describing exactly "
            "how this person texts, so another writer could imitate them "
            "perfectly. Cover: language(s) used and code-switching, typical "
            "message length, capitalization, punctuation habits, emoji/emoticon "
            "usage and which ones, slang/filler words/signature phrases, "
            "greetings & sign-offs, tone, and anything distinctive. Be concrete "
            "and quote a few characteristic words/phrases. Output only the guide.\n\n"
            "MESSAGES:\n" + corpus
        )
        if pairs:
            user += "\n\nSOME REPLY PAIRS (incoming → their reply):\n" + pairs
        guide = await llm.chat(
            [{"role": "system", "content": sys},
             {"role": "user", "content": user}],
            temperature=0.4,
        )

    if not guide:
        guide = heuristic_guide(samples, owner_name)

    return guide, examples_text
