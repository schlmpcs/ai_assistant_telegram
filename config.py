"""Configuration for the personal Telegram chat assistant.

This bot connects to your *personal* Telegram account via Telegram Business /
Chat Automation and replies to your private 1:1 chats AS you, in your own
learned texting style. It uses any OpenAI-compatible LLM. There is no database
of customers, no payments, and no proactive outreach — just you, your voice,
and your private chats.
"""

from __future__ import annotations

from typing import List

from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram (this assistant bot — a NEW bot, created via @BotFather) ---
    bot_token: SecretStr = Field(..., description="Token of the assistant bot")
    owner_ids: List[int] = Field(
        ...,
        description="Telegram user IDs allowed to configure this bot "
        "(your own account(s))",
    )

    # --- Who the bot is impersonating (used in the prompt) ---
    owner_name: str = Field(
        "me",
        description="Your name/handle. Used in the prompt, e.g. "
        "'You are texting as <owner_name>'.",
    )

    # --- LLM (any OpenAI-compatible API: OpenAI, OpenRouter, or local Ollama) ---
    llm_base_url: str = Field(
        "https://api.openai.com/v1",
        description="OpenAI-compatible base URL. "
        "OpenRouter: https://openrouter.ai/api/v1 | "
        "Ollama: http://host.docker.internal:11434/v1",
    )
    llm_api_key: SecretStr = Field(
        ...,
        description="API key for the provider above. For local Ollama any "
        "non-empty placeholder works (e.g. 'ollama').",
    )
    llm_models: List[str] = Field(
        default_factory=lambda: ["gpt-4o-mini"],
        description="Ordered model chain; on 429/error the next is tried",
    )
    llm_timeout: int = Field(60, description="LLM request timeout (s)")
    llm_max_tokens: int = Field(280, description="Max tokens per reply")

    # --- How many few-shot example pairs to inject into the prompt ---
    style_num_examples: int = Field(
        25, description="Number of learned example message pairs to inject"
    )

    # --- This bot's own small state store (SQLite) ---
    local_db_path: str = Field(
        "./data/assistant.db",
        description="SQLite file for connections, history, drafts, style",
    )

    # --- Behaviour ---
    auto_reply: bool = Field(
        True,
        description="Auto-send AI replies to incoming messages "
        "(sensitive topics are always drafted for approval regardless)",
    )
    reply_rate_per_min: int = Field(
        15,
        description="Abuse guard: max AI replies to one contact per minute; "
        "further messages in the window are recorded but not answered.",
    )
    reply_rate_per_hour: int = Field(
        150, description="Abuse guard: max AI replies to one contact per hour"
    )
    reply_debounce_seconds: float = Field(
        2.5,
        description="Wait this long for more messages before replying, so a "
        "burst of quick messages is answered all at once.",
    )
    manager_takeover_hours: int = Field(
        24,
        description="When you reply to a chat by hand, stay silent in that chat "
        "for this many hours so the bot doesn't talk over you. Each new manual "
        "message resets the window.",
    )


settings = Settings()
