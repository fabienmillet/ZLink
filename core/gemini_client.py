"""AIClient — génère des annonces textuelles pour le bandeau ZEvent.

Supporte Gemini (Google) et OpenAI selon la config :
    {"ai_provider": "gemini"|"openai", "ai_model": "...",
     "gemini_api_key": "...", "openai_api_key": "..."}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from core.paths import CONFIG_PATH as _CONFIG_PATH


def _env_key(provider: str) -> str:
    """Clé API depuis l'environnement (.env), prioritaire sur config.json.

    Permet de garder les secrets hors de config.json, qui est un fichier de
    préférences destiné à être partagé/sauvegardé.
    """
    import os
    var = "OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY"
    return os.environ.get(var, "").strip()


def _load_ai_config() -> tuple[str, str, str]:
    """Retourne (provider, model, api_key) : environnement d'abord, sinon config.json."""
    try:
        cfg: Any = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        provider = cfg.get("ai_provider", "gemini").lower()
        if provider == "openai":
            key = _env_key("openai") or cfg.get("openai_api_key", "")
            model = cfg.get("ai_model", "gpt-4o-mini")
        else:
            provider = "gemini"
            key = _env_key("gemini") or cfg.get("gemini_api_key", "")
            model = cfg.get("ai_model", "gemini-2.0-flash")
        return provider, model, str(key) if key else ""
    except Exception as exc:
        logger.warning("AIClient: impossible de lire config.json — %s", exc)
        return "gemini", "gemini-2.0-flash", ""


class GeminiClient:
    """Client IA générique — Gemini ou OpenAI selon config.json.

    Nom conservé pour compatibilité avec les imports existants.
    La config est relue à chaque appel pour refléter les changements en live.
    """

    def __init__(self) -> None:
        provider, _, key = _load_ai_config()
        if key:
            logger.info("AIClient: clé %s chargée depuis config.json", provider)
        else:
            logger.info("AIClient: pas de clé API — mode fallback local")

    # -- private ----------------------------------------------------------------

    def _fallback(self, context: dict[str, Any]) -> str:
        return (
            f"💚 {context.get('donation', '?')} récoltés — "
            f"{context.get('live_count', '?')} streamers en live — "
            f"{context.get('viewers', '?')} viewers connectés"
        )

    def _build_prompt(self, context: dict[str, Any]) -> str:
        return (
            f"Tu es le système d'annonce du ZEvent {context.get('year', 2025)}, "
            "événement caritatif Twitch français.\n"
            "En UNE phrase courte et percutante (maximum 120 caractères), résume "
            "l'information la plus importante en ce moment parmi :\n"
            f"- Cagnotte : {context.get('donation', '?')} "
            f"(+{context.get('delta', '?')} en 30 dernières minutes)\n"
            f"- Viewers : {context.get('viewers', '?')}\n"
            f"- Streamers live : {context.get('live_count', '?')}/{context.get('total_count', '?')}\n"
            f"- Prochain event : {context.get('next_event', 'aucun')}\n"
            f"- Goals récemment atteints : {context.get('recent_goals', 'aucun')}\n"
            f"- Top streamer : {context.get('top_streamer', '?')} "
            f"({context.get('top_viewers', '?')} viewers)\n"
            "Sois factuel, enthousiaste, en français. "
            "Utilise des emojis avec parcimonie. Pas de hashtag. Pas de mention @. "
            "Juste l'annonce."
        )

    # -- public -----------------------------------------------------------------

    async def generate_announcement(self, context: dict[str, Any]) -> str:
        """Génère une phrase d'annonce. Retourne le fallback si l'IA est indisponible."""
        provider, model, api_key = _load_ai_config()
        if not api_key:
            return self._fallback(context)

        import httpx

        prompt = self._build_prompt(context)

        try:
            async with httpx.AsyncClient() as client:
                if provider == "openai":
                    r = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 150,
                        },
                        timeout=5,
                    )
                    r.raise_for_status()
                    text: str = r.json()["choices"][0]["message"]["content"].strip()
                else:
                    r = await client.post(
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{model}:generateContent?key={api_key}",
                        json={"contents": [{"parts": [{"text": prompt}]}]},
                        timeout=5,
                    )
                    r.raise_for_status()
                    text = (
                        r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    )

                logger.debug("AIClient (%s/%s): annonce générée (%d chars)", provider, model, len(text))
                return text
        except Exception as exc:
            logger.warning("AIClient: erreur API %s — %s", provider, exc)
            return self._fallback(context)
