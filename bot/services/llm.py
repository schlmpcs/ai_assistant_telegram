"""Async LLM client over any OpenAI-compatible Chat Completions endpoint.

One code path serves three providers — switch purely via `.env`:

  • OpenAI      LLM_BASE_URL=https://api.openai.com/v1      LLM_MODELS=["gpt-4o-mini"]
  • OpenRouter  LLM_BASE_URL=https://openrouter.ai/api/v1   LLM_MODELS=["...:free", ...]
  • Ollama      LLM_BASE_URL=http://host.docker.internal:11434/v1  (local GPU)

`LLM_MODELS` is an ordered fallback chain: on a 429 (or any transient failure)
the next model is tried. Strictly async (aiohttp); returns None on total
failure so callers can degrade gracefully.
"""

import logging
from typing import Optional

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

# Statuses that mean "the whole attempt is doomed — stop rotating".
# Anything else (429, 404, 5xx, timeout) just tries the next model.
_FATAL_STATUSES = {401, 403}


async def chat(messages: list[dict], *, temperature: float = 0.6) -> Optional[str]:
    """Call chat-completions, rotating through the model chain on failure.

    `messages` is a list of {role, content}. Returns the assistant text from the
    first model that answers, or None if every model fails.
    """
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}",
        "Content-Type": "application/json",
        # Attribution headers OpenRouter recommends; ignored by OpenAI/Ollama.
        "HTTP-Referer": "https://github.com/schlmpcs/ai_assistant_telegram",
        "X-Title": "ai-assistant-telegram",
    }
    timeout = aiohttp.ClientTimeout(total=settings.llm_timeout)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for model in settings.llm_models:
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "temperature": temperature,
                    "max_tokens": settings.llm_max_tokens,
                }
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            choices = data.get("choices") or []
                            content = (
                                (choices[0].get("message") or {}).get("content", "")
                                if choices
                                else ""
                            ).strip()
                            if content:
                                return content
                            logger.warning("%s returned empty content; trying next", model)
                            continue

                        body = await resp.text()
                        if resp.status in _FATAL_STATUSES:
                            logger.error(
                                "LLM auth error %s (%s) — aborting chain: %s",
                                resp.status, model, body,
                            )
                            return None
                        # 429 / 404 / 5xx → rotate to the next model.
                        logger.warning(
                            "LLM %s on %s; trying next model: %s",
                            resp.status, model, body[:200],
                        )
                except aiohttp.ClientError as e:
                    logger.warning("Network error on %s; trying next model: %s", model, e)
                except Exception as e:  # noqa: BLE001 — never let one model kill the chain
                    logger.warning("Unexpected error on %s; trying next model: %s", model, e)
    except Exception as e:  # noqa: BLE001 — session-level failure
        logger.error("LLM call failed: %s", e)
        return None

    logger.error("All %d LLM models failed", len(settings.llm_models))
    return None
