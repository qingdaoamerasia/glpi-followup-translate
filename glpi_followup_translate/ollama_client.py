"""Ollama API client for text translation using local LLM."""

import logging
from typing import Optional

import requests

from .config import OllamaConfig

logger = logging.getLogger(__name__)


class OllamaClient:
    """Client for interacting with Ollama API for translation."""

    def __init__(self, config: OllamaConfig):
        self.config = config
        self.api_url = config.api_url.rstrip("/")
        self.model = config.model
        self.timeout = config.timeout
        self.session = requests.Session()

    def is_available(self) -> bool:
        """Check if Ollama is running and the model is available.

        Returns:
            True if Ollama is reachable and model exists
        """
        try:
            resp = self.session.get(
                f"{self.api_url}/api/tags", timeout=10
            )
            resp.raise_for_status()
            models = resp.json().get("models", [])
            model_names = [m.get("name", "") for m in models]
            # Check if our model (or a variant) is available
            for name in model_names:
                if self.model in name or name.startswith(self.model.split(":")[0]):
                    logger.info("Ollama model found: %s", name)
                    return True
            logger.warning(
                "Model '%s' not found. Available models: %s",
                self.model,
                model_names,
            )
            return False
        except requests.RequestException as e:
            logger.error("Ollama not available: %s", e)
            return False

    def translate(
        self, text: str, source_lang: str, target_lang: str,
        preserve_html: bool = False,
    ) -> Optional[str]:
        """Translate text using Ollama LLM.

        Args:
            text: Text to translate
            source_lang: Source language code (e.g., 'zh-cn', 'en')
            target_lang: Target language code (e.g., 'en', 'zh-cn')
            preserve_html: If True, preserve all HTML tags; only translate text content

        Returns:
            Translated text, or None if translation failed
        """
        lang_names = {
            "zh-cn": "Chinese (Simplified)",
            "zh": "Chinese",
            "en": "English",
        }
        src_name = lang_names.get(source_lang, source_lang)
        tgt_name = lang_names.get(target_lang, target_lang)

        if preserve_html:
            prompt = (
                f"You are a professional translator. "
                f"Translate ALL of the following HTML content from {src_name} to {tgt_name}. "
                f"It is CRITICAL that you translate EVERY word — do NOT copy the original text. "
                f"Preserve ALL HTML tags, attributes, and structure exactly as they are. "
                f"Only translate the visible text between tags. "
                f"Do NOT add, remove, or modify any HTML tags. "
                f"Return ONLY the translated HTML, no explanations.\n\n"
                f"HTML to translate:\n{text}"
            )
        else:
            prompt = (
                f"You are a professional translator. "
                f"Translate the following text from {src_name} to {tgt_name}. "
                f"It is CRITICAL that you translate EVERY word — do NOT copy the original text. "
                f"Return ONLY the translated text, no explanations.\n\n"
                f"Text to translate:\n{text}"
            )

        try:
            logger.debug(
                "Translating %d chars: %s -> %s", len(text), source_lang, target_lang
            )
            resp = self.session.post(
                f"{self.api_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,
                        "repeat_penalty": 1.2,
                    },
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            result = resp.json()

            translated = result.get("response", "").strip()
            if not translated:
                logger.warning("Ollama returned empty translation")
                return None

            logger.debug("Translation result: %s", translated[:100])
            return translated

        except requests.Timeout:
            logger.error(
                "Ollama translation timed out after %ds for %d chars",
                self.timeout,
                len(text),
            )
            return None
        except requests.RequestException as e:
            logger.error("Ollama translation failed: %s", e)
            return None
