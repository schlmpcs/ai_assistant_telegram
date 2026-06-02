# CLAUDE.md — architecture guide

Personal Telegram chat assistant: replies in the owner's **private 1:1 chats AS
the owner**, in their **learned texting style**. Adapted from a business
support bot, with all payments/grounding/proactive logic removed.

## Tech stack
- **aiogram 3.x** — Telegram Bot API, using Telegram **Business** updates
  (`business_connection`, `business_message`) to read and send as the owner.
- **aiosqlite** — the only datastore; a small local SQLite file. No Postgres.
- **OpenAI-compatible LLM** — one aiohttp code path for OpenAI / OpenRouter /
  Ollama, switched via `.env`. Ordered model fallback chain.
- **pydantic-settings** — config from `.env`.

## File map
- `main.py` — init store, seed default style, build bot, register routers, poll.
  No scheduler.
- `config.py` — `Settings` (pydantic). No DB_*, no pricing, no proactive_*.
- `ingest.py` — Telegram export parser + style profiler. CLI **and** importable
  (`run_ingest`). Robust to truncated JSON (salvage mode).
- `bot/handlers/business.py` — business connection + incoming-message handling;
  debounced reply generation; manual-takeover + abuse guards.
- `bot/handlers/owner.py` — owner commands (`/start /status /style /learn /auto
  /samples`) + draft approval callbacks + `/learn` export upload.
- `bot/services/llm.py` — async LLM client.
- `bot/services/dispatch.py` — deliver as owner, or draft for approval.
- `bot/services/style.py` — `build_style_profile`, `select_examples`,
  `format_examples`, heuristic fallback guide, emoji detection.
- `bot/db/local.py` — SQLite schema + async helpers.
- `bot/utils/prompts.py` — system prompt, escalation triggers, `[[ASK_OWNER]]`.
- `bot/utils/states.py` — `OwnerStates` FSM.
- `bot/utils/keyboards.py` — draft Send/Edit/Skip keyboard.

## Message flow
1. `business_message` arrives. Owner's own outgoing messages are recorded for
   context and (if hand-typed) open a quiet "takeover" window.
2. A contact's message is stored; if the owner recently replied by hand, or the
   contact is flooding (rate guard), the bot stays silent.
3. Otherwise the message is debounced (`reply_debounce_seconds`) so a burst is
   answered once. Then: load the owner's style guide + few-shot examples, build
   the system prompt, send history to the LLM.
4. The reply is auto-sent (as the owner) or drafted for approval. Sensitive
   topics (`needs_escalation`) always draft. `[[ASK_OWNER]]` → send a holding
   line and ping the owner.

## Style-learning pipeline
`ingest` → keep owner's 1:1 messages, drop sensitive/low-quality ones →
`style_samples` table → `build_style_profile` writes an LLM **style guide** +
selects a diverse **few-shot example block** → stored in `owner_settings`
(`style_prompt`, `style_examples`) → injected into `system_prompt` on every reply.

## Conventions
- **Strictly async** throughout (aiogram + aiosqlite + aiohttp).
- Contact-facing sends go **only** through `dispatch` (`deliver` /
  `send_or_approve`) so the HTML-parse fallback and draft flow are consistent.
- The model must **never invent personal facts** about the owner → it emits
  `[[ASK_OWNER]]` and the owner is pinged.
- Python 3.9-compatible: modules using `int | None` / `list[dict]` hints in
  signatures start with `from __future__ import annotations`.
- **Git commits: no co-authorship trailers** (no `Co-Authored-By`, no
  "Generated with" lines) — matches the author's sibling repos.
