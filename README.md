# Personal Telegram Chat Assistant

A personal Telegram assistant that replies in your **private 1:1 chats AS you**,
in **your own learned texting style**. It connects to your account via Telegram
Business, learns how you write from a Telegram Desktop export, and either
auto-sends replies or drafts them for one-tap approval.

It is *not* a customer-support bot: no database of customers, no payments, no
proactive messaging. Just you, your voice, and your private chats.

## How it works

```
incoming private message  ──►  Telegram Business  ──►  this bot
                                                         │
                                  load your style guide + few-shot examples
                                                         │
                                            LLM writes the reply AS you
                                                         │
                              ┌──────────────────────────┴───────────────────┐
                         AUTO_REPLY=true                              draft for approval
                       send as you immediately                  (sensitive topics always)
```

Your texting style is learned by `ingest.py`:

```
result.json (Telegram export)
        │  parse, keep only YOUR messages from 1:1 personal chats
        │  strip emails / cards / passwords / link-only messages
        ▼
   style_samples (SQLite)
        │  LLM writes a STYLE GUIDE  +  a diverse FEW-SHOT EXAMPLE set
        ▼
   injected into the system prompt every reply
```

## Setup

1. **Create the bot.** Talk to [@BotFather](https://t.me/BotFather), create a new
   bot, and grab its token. In BotFather, enable **Business Mode** for the bot
   (`/mybots → Bot Settings → Business Mode`).
2. **Pick an LLM.** OpenAI (`gpt-4o-mini`, default), OpenRouter (free models), or
   a local Ollama. See `.env.example`.
3. **Configure.** `cp .env.example .env` and fill in `BOT_TOKEN`, `OWNER_IDS`
   (your own Telegram user id), `OWNER_NAME`, and the LLM settings.
4. **Install.** `pip install -r requirements.txt` (or use Docker — see below).
5. **Teach it your style.** Export your chats in Telegram Desktop
   (Settings → Advanced → Export Telegram data → format **JSON**), then either:
   - **CLI (recommended for large exports):**
     ```
     python ingest.py "path/to/result.json" --owner-id <your id> --owner-name <name>
     ```
     Add `--no-llm` to build the style guide offline from statistics only.
   - **In chat (small exports):** send `/learn` to the bot and upload
     `result.json` as a file. Telegram caps bot downloads at ~20 MB, so big
     exports must use the CLI.
6. **Run.** `python main.py` (or `docker compose up -d --build`).
7. **Connect.** Telegram → Settings → **Telegram Business** → **Chat-bots** →
   add your bot, choose chats, and enable **"Reply to messages"**.

## Auto vs draft

- `AUTO_REPLY=true` (default): replies are sent as you immediately.
- `/auto off`: replies are held as drafts; you get **Send / Edit / Skip** buttons.
- **Sensitive topics always draft** regardless of the setting.

## The `[[ASK_OWNER]]` safety behavior

The model only knows what it was given — it does **not** know your real
location, schedule, plans, or feelings. If a reply would require inventing such
a fact, or asks you to commit to something (meeting up, a time/place, money, a
promise), the model emits a hidden `[[ASK_OWNER]]` marker. The contact gets a
short, natural holding line ("секунду, отвечу чуть позже") and **you are pinged**
to handle it yourself. It will never agree to send money, share
passwords/codes, or share your address on your behalf.

## Privacy

Ingestion uses **only your 1:1 personal chats** (`type: personal_chat`) and only
**your own** messages. It strips anything that looks like an email, bank card,
long digit run, phone number, password/login dump, or a link-only message.
Exports can be large and are sometimes truncated mid-file — the CLI handles both
(it salvages as many messages as it can from a corrupt file). Your `.env`, the
SQLite DB, and any uploaded exports stay local and are git-ignored.

## Commands

| Command    | What it does                                                        |
|------------|---------------------------------------------------------------------|
| `/start`   | Intro + how to connect and how to teach your style                  |
| `/status`  | Connection state, auto-reply on/off, style set?, # learned samples  |
| `/style`   | Type a manual style guide that overrides the learned one            |
| `/learn`   | Upload a Telegram export to learn your style (small exports)         |
| `/auto`    | `on` / `off` — toggle auto-send vs draft-for-approval               |
| `/samples` | How many style samples have been learned                            |
