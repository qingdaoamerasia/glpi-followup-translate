"""Ollama API client for text translation using local LLM."""

import logging
import re
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
        glossary: dict = None,
    ) -> Optional[str]:
        """Translate text using Ollama LLM.

        Args:
            text: Text to translate (may contain HTML)
            source_lang: Source language code (e.g., 'zh-cn', 'en')
            target_lang: Target language code (e.g., 'en', 'zh-cn')
            glossary: Accepted but not used in prompt — glossary enforcement is
                      handled by post-processing in main.py for reliability
                      with small translation models.

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

        prompt = (
            f"{src_name} to {tgt_name} translation:\n\n"
            f"{text}\n\n"
            f"{tgt_name}:"
        )

        try:
            logger.debug(
                "Translating %d chars: %s -> %s", len(text), source_lang, target_lang
            )
            # Dynamic timeout: at least config value, more for longer text
            dynamic_timeout = max(self.timeout, len(text) / 10)
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
                timeout=dynamic_timeout,
            )
            resp.raise_for_status()
            result = resp.json()

            translated = result.get("response", "").strip()
            if not translated:
                logger.warning("Ollama returned empty translation")
                return None

            # Clean up model artifacts
            translated = self._clean_output(translated)
            if not translated:
                logger.warning("Ollama translation was empty after cleanup")
                return None

            logger.debug("Translation result: %s", translated[:100])
            return translated

        except requests.Timeout:
            logger.error(
                "Ollama translation timed out after %.0fs for %d chars",
                dynamic_timeout, len(text),
            )
            return None
        except requests.RequestException as e:
            logger.error("Ollama translation failed: %s", e)
            return None

    @staticmethod
    def _clean_output(text: str) -> str:
        """Strip common artifacts from small translation model output.

        Small models sometimes append instruction echoes, language labels,
        or glossary-like lists after the actual translation.
        """
        if not text:
            return text

        # Remove trailing instruction-like blocks (e.g. "Use these term translations...")
        # These patterns indicate the model echoed back prompt instructions
        noise_patterns = [
            r'\n+Use these term translations.*$',
            r'\n+请始终使用以下术语翻译.*$',
            r'\n+请使用以下术语.*$',
            r'\n+以下是术语.*$',
            r'\n+Term translations:.*$',
            r'\n+Glossary:.*$',
        ]
        for pattern in noise_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)

        # Remove trailing language labels like "Chinese (Simplified):" or "English:"
        text = re.sub(r'\n+(?:Chinese(?:\s*\([^)]*\))?|English)\s*:\s*$', '', text, flags=re.IGNORECASE)

        return text.strip()
