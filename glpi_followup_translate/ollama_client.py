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
        glossary_hint: bool = False,
    ) -> Optional[str]:
        """Translate text using Ollama LLM.

        Args:
            text: Text to translate (may contain HTML)
            source_lang: Source language code (e.g., 'zh-cn', 'en')
            target_lang: Target language code (e.g., 'en', 'zh-cn')
            glossary: Accepted but not used in prompt — glossary enforcement is
                      handled by post-processing in main.py for reliability
                      with small translation models.
            glossary_hint: If True, add a prompt instruction telling the model
                           to preserve [GLS:N] placeholder tokens as-is.

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

        hint_line = ""
        if glossary_hint:
            hint_line = "Do not translate tokens like [GLS:N], keep them exactly as-is.\n\n"

        prompt = (
            f"{src_name} to {tgt_name} translation:\n"
            f"{hint_line}"
            f"{text}\n\n"
            f"{tgt_name}:"
        )

        try:
            logger.debug(
                "Translating %d chars: %s -> %s", len(text), source_lang, target_lang
            )
            # Dynamic timeout: at least config value, more for longer text
            # ~15 chars/second for 1.8B model on typical hardware
            dynamic_timeout = max(self.timeout, len(text) / 15)
            resp = self.session.post(
                f"{self.api_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,
                        "repeat_penalty": 1.2,
                        "num_ctx": 8192,
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

        Small models sometimes prepend or append instruction echoes,
        language labels, or glossary-like lists around the translation.
        """
        if not text:
            return text

        # --- Stage 1: Remove trailing instruction/glossary echoes ---
        # These patterns indicate the model echoed back prompt instructions
        noise_patterns = [
            r'\n+Use these term translations.*$',
            r'\n+请始终使用以下术语翻译.*$',
            r'\n+请使用以下术语.*$',
            r'\n+以下是术语.*$',
            r'\n+Term translations:.*$',
            r'\n+Glossary:.*$',
            # Chinese-direction instruction echoes after translation
            r'\n+英语到中文（简化版）翻译.*$',
            r'\n+英语到中文翻译.*$',
            r'\n+中文到英语翻译.*$',
            r'\n+中文（简体）到英语翻译.*$',
            r'\n+简化版英文翻译.*$',
            r'\n+简体中文翻译.*$',
            # English prompt echo used as trailing label
            r'\n+Chinese\s*(?:\([^)]*\))?\s*[:：].*$',
            r'\n+English\s*[:：].*$',
        ]
        for pattern in noise_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)

        # Remove trailing language labels like "Chinese (Simplified):" or "English:"
        text = re.sub(
            r'\n+(?:Chinese(?:\s*\([^)]*\))?|English)\s*:\s*$', '',
            text, flags=re.IGNORECASE,
        )

        # --- Stage 1b: Remove inline parenthetical noise ---
        # The model sometimes appends alternative translations in parentheses
        # without a leading newline, e.g. "（简化版英文翻译：...）"
        paren_noise = [
            r'[（(]\s*简化版[英中][文语]?\s*翻译\s*[:：][^)）]*[)）]',
            r'[（(]\s*完整[英中][文语]?\s*翻译\s*[:：][^)）]*[)）]',
            r'[（(]\s*另一种?翻译\s*[:：][^)）]*[)）]',
        ]
        for pattern in paren_noise:
            text = re.sub(pattern, '', text, flags=re.DOTALL)

        # Inline trailing echo without newline prefix
        inline_echo = [
            r'\n+\s*简化版[英中][文语]?\s*翻译\s*[:：].*$',
            r'\n+\s*完整[英中][文语]?\s*翻译\s*[:：].*$',
        ]
        for pattern in inline_echo:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)

        # --- Stage 2: Remove leading instruction echoes ---
        # The model sometimes parrots the prompt header at the start of output,
        # e.g. "Chinese (Simplified) to English translation: The server..."
        # or the Chinese equivalent "英语到中文（简化版）翻译：服务器..."
        leading_patterns = [
            # English prompt echo: "Chinese (Simplified) to English translation:"
            r'^(?:Chinese(?:\s*\([^)]*\))?\s+to\s+(?:Chinese(?:\s*\([^)]*\))?|English)\s+translation)\s*[:：]\s*',
            # English prompt echo: "English to Chinese (Simplified) translation:"
            r'^(?:English\s+to\s+Chinese(?:\s*\([^)]*\))?\s+translation)\s*[:：]\s*',
            # Chinese-direction echoes (model outputs Chinese label for en↔zh)
            r'^(?:英语到中文（简化版）翻译|英语到中文翻译|中文到英语翻译|中文（简体）到英语翻译|中文到英语翻译)\s*[:：]\s*',
            # Bare "简化版" label prefix
            r'^简化版[英中][文语]?\s*翻译\s*[:：]\s*',
            # Glossary hint instruction echoes (model translates the instruction)
            r'^(?:Do not translate tokens like \[GLS:N\][^\n]*\n\n?)',
            r'^(?:不[要需]翻译.*?\[GLS[^\]]*\][^\n]*\n\n?)',
            r'^(?:请保留.*?\[GLS[^\]]*\][^\n]*\n\n?)',
            # Bare language label prefix: "Chinese (Simplified): <content>"
            # Only strip when followed by actual content (not at end of string)
            r'^(?:Chinese(?:\s*\([^)]*\))?)\s*[:：]\s*(?=[^\n])',
            r'^English\s*[:：]\s*(?=[^\n])',
        ]
        for pattern in leading_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        return text.strip()
