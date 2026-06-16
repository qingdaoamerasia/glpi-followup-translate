"""Main daemon loop for GLPI Followup Translate."""

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from typing import Set, Optional

from langdetect import detect, LangDetectException

from .config import AppConfig, load_config
from .glpi_client import GlpiClient
from .ollama_client import OllamaClient

logger = logging.getLogger("glpi_followup_translate")

# HTML tag pattern for stripping
_HTML_RE = re.compile(r"<[^>]*>")


def strip_html(text: str) -> str:
    """Remove HTML tags and HTML entities from text."""
    text = _HTML_RE.sub("", text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return text.strip()


# Pattern to strip inline styles and verbose attributes from HTML tags
_STYLE_RE = re.compile(r'\s*style\s*=\s*"[^"]*"', re.IGNORECASE)
_CLASS_RE = re.compile(r'\s*class\s*=\s*"[^"]*"', re.IGNORECASE)
_ID_RE = re.compile(r'\s*id\s*=\s*"[^"]*"', re.IGNORECASE)
_DATA_RE = re.compile(r'\s*data-\w+\s*=\s*"[^"]*"', re.IGNORECASE)
_CSSVAR_RE = re.compile(r'\s*--[\w-]+\s*:\s*[^;"]+;?', re.IGNORECASE)


def strip_html_styles(html: str) -> str:
    """Remove inline styles, classes, ids, and data attrs to reduce token count.

    Keeps structural HTML tags (<p>, <strong>, <em>, <h3>, etc.) intact.
    Only removes verbose attributes that bloat the text without adding meaning.
    """
    html = _STYLE_RE.sub('', html)
    html = _CLASS_RE.sub('', html)
    html = _ID_RE.sub('', html)
    html = _DATA_RE.sub('', html)
    return html

# Graceful shutdown flag
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s, shutting down...", signum)
    _shutdown = True


def setup_logging(config: AppConfig) -> None:
    """Configure logging based on config."""
    log_level = getattr(logging, config.logging.level.upper(), logging.INFO)
    log_file = config.logging.file

    # Fix Windows console encoding for non-ASCII characters
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(log_level)

    # File handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(log_level)

    logger.setLevel(log_level)
    logger.addHandler(console)
    logger.addHandler(file_handler)


class ProcessedState:
    """Track which items have been processed to avoid re-translation.

    Uses content hashes so that if content changes, the item is retried.
    """

    def __init__(self, state_file: str = "processed_state.json"):
        self.state_file = state_file
        # Maps followup_id -> content hash
        self.processed_followups: dict[int, str] = {}
        # Maps ticket_id -> (name_hash, content_hash)
        self.processed_tickets: dict[int, tuple[str, str]] = {}
        # Maps task_id -> content hash
        self.processed_tasks: dict[int, str] = {}
        # Maps solution_id -> content hash
        self.processed_solutions: dict[int, str] = {}
        # Maps validation_id -> (submission_hash, approval_hash)
        self.processed_validations: dict[int, tuple[str, str]] = {}
        self._load()

    def _load(self) -> None:
        """Load processed data from file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    # Handle old format (list of IDs) and new format (dict with hashes)
                    raw_fu = data.get("followups", [])
                    if isinstance(raw_fu, list):
                        # Old format: list of IDs, convert to dict with empty hashes
                        self.processed_followups = {
                            int(k): "" for k in raw_fu if isinstance(k, (int, str))
                        }
                    else:
                        self.processed_followups = {
                            int(k): str(v) for k, v in raw_fu.items()
                        }
                    raw_tickets = data.get("tickets", [])
                    if isinstance(raw_tickets, list):
                        self.processed_tickets = {
                            int(k): ("", "") for k in raw_tickets if isinstance(k, (int, str))
                        }
                    else:
                        self.processed_tickets = {}
                        for k, v in raw_tickets.items():
                            if isinstance(v, list) and len(v) == 2:
                                self.processed_tickets[int(k)] = (str(v[0]), str(v[1]))
                            else:
                                self.processed_tickets[int(k)] = ("", "")
                    # Load tasks, solutions, validations (new in 0.1.1)
                    self.processed_tasks = {
                        int(k): str(v) for k, v in data.get("tasks", {}).items()
                    }
                    self.processed_solutions = {
                        int(k): str(v) for k, v in data.get("solutions", {}).items()
                    }
                    self.processed_validations = {
                        int(k): (
                            str(v[0]) if isinstance(v, list) and len(v) == 2 else str(v),
                            str(v[1]) if isinstance(v, list) and len(v) == 2 else "",
                        )
                        for k, v in data.get("validations", {}).items()
                    }
                logger.info(
                    "Loaded state: %d followups, %d tickets, %d tasks, %d solutions, %d validations",
                    len(self.processed_followups),
                    len(self.processed_tickets),
                    len(self.processed_tasks),
                    len(self.processed_solutions),
                    len(self.processed_validations),
                )
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Failed to load state file, starting fresh: %s", e)

    def save(self) -> None:
        """Save processed data to file."""
        try:
            with open(self.state_file, "w") as f:
                json.dump(
                    {
                        "followups": {str(k): v for k, v in self.processed_followups.items()},
                        "tickets": {
                            str(k): [v[0], v[1]]
                            for k, v in self.processed_tickets.items()
                        },
                        "tasks": {str(k): v for k, v in self.processed_tasks.items()},
                        "solutions": {str(k): v for k, v in self.processed_solutions.items()},
                        "validations": {
                            str(k): [v[0], v[1]]
                            for k, v in self.processed_validations.items()
                        },
                    },
                    f,
                )
        except IOError as e:
            logger.error("Failed to save state file: %s", e)

    def _content_hash(self, text: str) -> str:
        """Compute a short hash of the content."""
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]

    def is_followup_processed(self, followup_id: int, content: str) -> bool:
        """Check if a followup has been processed with the same content."""
        if followup_id not in self.processed_followups:
            return False
        old_hash = self.processed_followups[followup_id]
        new_hash = self._content_hash(content)
        return old_hash == new_hash

    def mark_followup_processed(self, followup_id: int, content: str) -> None:
        """Mark a followup as processed with its current content."""
        self.processed_followups[followup_id] = self._content_hash(content)

    def is_ticket_processed(self, ticket_id: int, name: str, content: str) -> bool:
        """Check if a ticket has been processed with the same name and content."""
        if ticket_id not in self.processed_tickets:
            return False
        old_names_hash, old_content_hash = self.processed_tickets[ticket_id]
        return (
            old_names_hash == self._content_hash(name)
            and old_content_hash == self._content_hash(content)
        )

    def mark_ticket_processed(self, ticket_id: int, name: str, content: str) -> None:
        """Mark a ticket as processed with its current name and content."""
        self.processed_tickets[ticket_id] = (
            self._content_hash(name),
            self._content_hash(content),
        )

    def is_task_processed(self, task_id: int, content: str) -> bool:
        """Check if a task has been processed with the same content."""
        if task_id not in self.processed_tasks:
            return False
        return self.processed_tasks[task_id] == self._content_hash(content)

    def mark_task_processed(self, task_id: int, content: str) -> None:
        """Mark a task as processed with its current content."""
        self.processed_tasks[task_id] = self._content_hash(content)

    def is_solution_processed(self, solution_id: int, content: str) -> bool:
        """Check if a solution has been processed with the same content."""
        if solution_id not in self.processed_solutions:
            return False
        return self.processed_solutions[solution_id] == self._content_hash(content)

    def mark_solution_processed(self, solution_id: int, content: str) -> None:
        """Mark a solution as processed with its current content."""
        self.processed_solutions[solution_id] = self._content_hash(content)

    def is_submission_processed(self, validation_id: int, sub_comment: str) -> bool:
        """Check if the submission_comment for a validation has been processed."""
        if validation_id not in self.processed_validations:
            return False
        old_hash = self.processed_validations[validation_id][0]
        return old_hash == self._content_hash(sub_comment)

    def is_approval_processed(self, validation_id: int, app_comment: str) -> bool:
        """Check if the approval_comment for a validation has been processed."""
        if validation_id not in self.processed_validations:
            return False
        old_hash = self.processed_validations[validation_id][1]
        return old_hash == self._content_hash(app_comment)

    def mark_submission_processed(self, validation_id: int, sub_comment: str) -> None:
        """Mark a validation's submission_comment as processed."""
        _, app_hash = self.processed_validations.get(validation_id, ("", ""))
        self.processed_validations[validation_id] = (self._content_hash(sub_comment), app_hash)

    def mark_approval_processed(self, validation_id: int, app_comment: str) -> None:
        """Mark a validation's approval_comment as processed."""
        sub_hash, _ = self.processed_validations.get(validation_id, ("", ""))
        self.processed_validations[validation_id] = (sub_hash, self._content_hash(app_comment))

    def cleanup_followups(self, valid_ids: Set[int]) -> None:
        """Remove followup IDs that no longer exist."""
        before = len(self.processed_followups)
        self.processed_followups = {
            k: v for k, v in self.processed_followups.items() if k in valid_ids
        }
        removed = before - len(self.processed_followups)
        if removed:
            logger.info("Cleaned up %d stale followup entries", removed)

    def cleanup_tickets(self, valid_ids: Set[int]) -> None:
        """Remove ticket IDs that no longer exist."""
        before = len(self.processed_tickets)
        self.processed_tickets = {
            k: v for k, v in self.processed_tickets.items() if k in valid_ids
        }
        removed = before - len(self.processed_tickets)
        if removed:
            logger.info("Cleaned up %d stale ticket entries", removed)

    def cleanup_tasks(self, valid_ids: Set[int]) -> None:
        """Remove task IDs that no longer exist."""
        before = len(self.processed_tasks)
        self.processed_tasks = {
            k: v for k, v in self.processed_tasks.items() if k in valid_ids
        }
        removed = before - len(self.processed_tasks)
        if removed:
            logger.info("Cleaned up %d stale task entries", removed)

    def cleanup_solutions(self, valid_ids: Set[int]) -> None:
        """Remove solution IDs that no longer exist."""
        before = len(self.processed_solutions)
        self.processed_solutions = {
            k: v for k, v in self.processed_solutions.items() if k in valid_ids
        }
        removed = before - len(self.processed_solutions)
        if removed:
            logger.info("Cleaned up %d stale solution entries", removed)

    def cleanup_validations(self, valid_ids: Set[int]) -> None:
        """Remove validation IDs that no longer exist."""
        before = len(self.processed_validations)
        self.processed_validations = {
            k: v for k, v in self.processed_validations.items() if k in valid_ids
        }
        removed = before - len(self.processed_validations)
        if removed:
            logger.info("Cleaned up %d stale validation entries", removed)


def detect_language(text: str) -> str:
    """Detect the language of the given text.

    Args:
        text: Text to detect language for

    Returns:
        Language code (e.g., 'zh-cn', 'zh', 'en', or 'unknown')
    """
    try:
        lang = detect(text)
        return lang.lower()
    except LangDetectException:
        return "unknown"


def _count_cjk(text: str) -> int:
    """Count CJK (Chinese) characters in text."""
    return sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')


def _cjk_ratio(text: str) -> float:
    """Calculate the ratio of CJK characters to total alphabetic characters."""
    alpha_chars = sum(1 for c in text if c.isalpha())
    if alpha_chars == 0:
        return 0.0
    cjk_count = _count_cjk(text)
    return cjk_count / alpha_chars


def detect_language_with_fallback(text: str, supported: set) -> str:
    """Detect language with fallback for short and mixed-language texts.

    langdetect can misidentify short English texts (e.g. "Yes, sure" -> fr),
    short Chinese texts (e.g. "测试" -> ko), and mixed Chinese-English texts.

    Heuristic using CJK character ratio:
    - If CJK ratio >= 10%: treat as zh-cn (clearly Chinese text, possibly with English terms)
    - If CJK ratio < 10% but CJK chars present: override langdetect 'en' to zh-cn
      (Chinese-speaking user writing mostly English — any CJK is a strong signal)
    - If no CJK and short ASCII: fall back to en
    """
    cjk_count = _count_cjk(text)
    ratio = _cjk_ratio(text)

    # High CJK ratio (>=10%): clearly Chinese text
    if ratio >= 0.1 and 'zh-cn' in supported:
        return 'zh-cn'

    # Standard langdetect
    lang = detect_language(text)
    if lang in supported:
        # Override: if CJK chars present but langdetect says 'en',
        # treat as zh-cn. CJK presence indicates a Chinese-speaking user
        # even when English words dominate the text.
        if cjk_count > 0 and lang == 'en' and 'zh-cn' in supported:
            return 'zh-cn'
        return lang

    # Low CJK ratio with langdetect returning unsupported language
    if cjk_count > 0 and 'zh-cn' in supported:
        return 'zh-cn'

    # If text is short and ASCII-only, it's likely English misidentified
    if len(text) < 50 and all(ord(c) < 128 for c in text) and 'en' in supported:
        return 'en'

    return lang


def is_already_translated(content: str, prefix: str) -> bool:
    """Check if content already contains a translation."""
    return prefix in content


def extract_original_text(content: str, prefix: str) -> str:
    """Extract the original text from content that may already have translation."""
    if prefix in content:
        parts = content.split(prefix, 1)
        return parts[0].strip()
    return content.strip()


def has_html_tags(text: str) -> bool:
    """Check if text contains HTML tags."""
    return bool(_HTML_RE.search(text))


def extract_outer_tag(html: str) -> str:
    """Extract the outermost HTML block tag from content.

    If the content is wrapped in a structural tag like <p>, <div>, etc.,
    return that tag name. Otherwise return None to indicate plain text.
    """
    stripped = html.strip()
    match = re.match(r'^<(\w+)[^>]*>', stripped, re.IGNORECASE)
    if not match:
        return None
    tag = match.group(1).lower()
    # Only consider structural/block-level tags for wrapping
    if tag in ("p", "div", "span", "section", "article", "blockquote", "pre"):
        return tag
    return None


def build_translated_content(original: str, translated: str, prefix: str) -> str:
    """Build content preserving original and adding translation.

    For HTML content: wraps [AUTO-TRANSLATED] in <strong> for bold formatting,
    with <br> line break after the marker.
    For plain text: uses plain marker with \\n separators.
    """
    if has_html_tags(original):
        return f"{original}\n\n<strong>{prefix}</strong><br>\n{translated}"
    else:
        return f"{original}\n\n{prefix}\n{translated}"


def build_translated_title(original: str, translated: str) -> str:
    """Build title preserving original and adding translation (slash-separated)."""
    return f"{original} / {translated}"


def _get_glossary(config: AppConfig, source_lang: str, target_lang: str) -> dict:
    """Get the glossary terms for a specific translation direction.

    Looks up glossary entries for source_lang first, then falls back to
    any matching base language (e.g., 'zh-cn' -> 'zh').
    """
    glossary = config.translation.glossary
    if not glossary:
        return {}

    # Direct match
    terms = dict(glossary.get(source_lang, {}))

    # Fallback: try base language (e.g., 'zh-cn' -> 'zh')
    if not terms and '-' in source_lang:
        base = source_lang.split('-')[0]
        terms = dict(glossary.get(base, {}))

    return terms


def _apply_glossary(text: str, glossary: dict) -> str:
    """Post-process translated text to enforce glossary terms.

    Replaces source terms found in the translation with their prescribed glossary
    translations. Uses word-boundary matching for English terms to avoid partial
    replacements. This handles the case where the model preserves the source term
    unchanged in the output.
    """
    if not glossary or not text:
        return text

    for src_term, tgt_term in glossary.items():
        if not src_term or not tgt_term:
            continue
        if src_term == tgt_term:
            continue
        if src_term in text:
            has_cjk_src = any('\u4e00' <= c <= '\u9fff' for c in src_term)
            if has_cjk_src:
                text = text.replace(src_term, tgt_term)
            else:
                text = re.sub(
                    r'\b' + re.escape(src_term) + r'\b',
                    tgt_term,
                    text,
                )

    return text


_GLS_PLACEHOLDER_RE = re.compile(r'GLS(\d+)GLS')


def _replace_with_placeholders(text: str, glossary: dict) -> tuple:
    """Replace glossary source terms with placeholders before translation.

    Returns:
        (modified_text, mapping) where mapping is {placeholder_id: target_term}
    """
    if not glossary or not text:
        return text, {}

    mapping = {}
    idx = 0
    # Sort by length descending so longer terms are replaced first
    # (prevents partial replacement of overlapping terms)
    for src_term, tgt_term in sorted(glossary.items(), key=lambda x: len(x[0]), reverse=True):
        if not src_term or not tgt_term or src_term == tgt_term:
            continue
        if src_term not in text:
            continue

        placeholder = f"GLS{idx}GLS"
        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in src_term)
        if has_cjk:
            text = text.replace(src_term, placeholder)
        else:
            text = re.sub(r'\b' + re.escape(src_term) + r'\b', placeholder, text)
        mapping[idx] = tgt_term
        idx += 1

    return text, mapping


def _restore_placeholders(text: str, mapping: dict) -> str:
    """Replace placeholders with glossary target terms after translation.

    Small models may add spaces (e.g. 'GLS 0 GLS') or change case,
    so we also try flexible regex patterns as fallback.
    """
    if not mapping or not text:
        return text

    for idx, tgt_term in mapping.items():
        placeholder = f"GLS{idx}GLS"
        # Try exact match first
        if placeholder in text:
            text = text.replace(placeholder, tgt_term)
        else:
            # Fallback: flexible regex for model artifacts (spaces, case)
            pattern = rf'GLS\s*{idx}\s*GLS'
            text = re.sub(pattern, tgt_term, text, flags=re.IGNORECASE)

    return text


def _translate_chunked(
    text: str, source_lang: str, target_lang: str, ollama: OllamaClient,
) -> Optional[str]:
    """Translate long text by splitting into paragraphs to avoid timeout.

    Splits on double-newline (paragraph) boundaries first, then sentence
    boundaries if paragraphs are still too long.
    """
    # Split on paragraph boundaries
    chunks = re.split(r'\n\n+', text)
    # If any chunk is still > 500 chars, split further on sentence boundaries
    final_chunks = []
    for chunk in chunks:
        if len(chunk) > 500:
            sentences = re.split(r'(?<=[。！？.!?])\s*', chunk)
            final_chunks.extend(s for s in sentences if s.strip())
        else:
            if chunk.strip():
                final_chunks.append(chunk)

    logger.debug("Chunked %d chars into %d parts", len(text), len(final_chunks))
    translated_parts = []
    for i, chunk in enumerate(final_chunks):
        result = ollama.translate(chunk.strip(), source_lang, target_lang)
        if result:
            translated_parts.append(result)
        else:
            logger.warning("Chunk %d/%d translation failed", i+1, len(final_chunks))
            translated_parts.append(chunk)  # keep original as fallback

    return "\n\n".join(translated_parts)


def process_text(
    text: str,
    item_id: int,
    item_type: str,
    config: AppConfig,
    ollama: OllamaClient,
) -> Optional[str]:
    """Process a text for translation.

    Args:
        text: Text to potentially translate (may contain HTML)
        item_id: ID of the item (for logging)
        item_type: Type of item ("followup" or "ticket")
        config: Application configuration
        ollama: Ollama API client

    Returns:
        Translated text if translation was needed, None otherwise.
        For HTML content, returns translated HTML with tags preserved.
    """
    # Strip HTML tags for language detection and length check
    plain_text = strip_html(text)

    if not plain_text or (config.translation.min_text_length > 0 and len(plain_text) < config.translation.min_text_length):
        return None

    # Skip if already translated
    if is_already_translated(text, config.translation.prefix):
        return None

    # Detect language using plain text (no HTML), with fallback for short ASCII
    source_lang = detect_language_with_fallback(
        plain_text, set(config.translation.source_languages)
    )
    cjk_r = _cjk_ratio(plain_text)
    logger.debug("%s %d detected language: %s (CJK ratio: %.1f%%)", item_type, item_id, source_lang, cjk_r * 100)

    # Check if it's a language we should translate
    target_lang = config.translation.target_language.get(source_lang)
    if not target_lang:
        logger.debug(
            "%s %d: language '%s' not in translation pairs, skipping",
            item_type,
            item_id,
            source_lang,
        )
        return None

    # Strip verbose HTML styles/classes to reduce tokens, but keep structural
    # tags (<p>, <strong>, <em>, etc.) so the translation preserves formatting.
    compact_text = strip_html_styles(text) if has_html_tags(text) else text

    if len(compact_text) > 8000:
        logger.warning(
            "%s %d: text too long (%d chars), skipping", item_type, item_id, len(compact_text)
        )
        return None

    logger.info(
        "Translating %s %d (%s -> %s, %d chars): %s...",
        item_type,
        item_id,
        source_lang,
        target_lang,
        len(compact_text),
        plain_text[:80],
    )

    # Get glossary for this translation direction
    glossary = _get_glossary(config, source_lang, target_lang)
    placeholder_mapping = {}
    if glossary:
        logger.debug("Using glossary with %d terms for %s -> %s", len(glossary), source_lang, target_lang)
        # Replace glossary source terms with placeholders BEFORE translation
        compact_text, placeholder_mapping = _replace_with_placeholders(compact_text, glossary)
        if placeholder_mapping:
            logger.debug("Replaced %d glossary term(s) with placeholders", len(placeholder_mapping))

    # For long texts, split into paragraphs to avoid timeout
    if len(compact_text) > 800:
        translated = _translate_chunked(compact_text, source_lang, target_lang, ollama)
    else:
        translated = ollama.translate(compact_text, source_lang, target_lang)
    if not translated:
        logger.warning("Translation failed for %s %d", item_type, item_id)
        return None

    # Post-process: restore placeholder terms with correct glossary targets
    if placeholder_mapping:
        translated = _restore_placeholders(translated, placeholder_mapping)

    # Post-process: also apply glossary as fallback for any surviving source terms
    if glossary:
        translated = _apply_glossary(translated, glossary)

    # Validate: if the model returned identical text, translation didn't happen.
    # This can occur with small models on short or HTML-preserved input.
    translated_plain = strip_html(translated)
    if translated_plain == plain_text:
        logger.warning("Translation for %s %d produced identical text, model may have failed", item_type, item_id)
        return None

    # Validate: ensure the translated text is actually in the target language,
    # not just a rephrasing in the source language (model failure mode).
    # Use CJK ratio comparison for zh<->en translations to handle mixed-language texts
    # where a few CJK chars may survive in the translation (e.g. proper nouns).
    translation_valid = False
    detected_lang = detect_language_with_fallback(
        translated_plain, set(config.translation.source_languages)
    )
    if detected_lang != source_lang:
        translation_valid = True
    else:
        # Language detection says same language, but check CJK ratio shift
        # for zh-cn <-> en translations: a significant ratio drop/gain indicates
        # the translation actually happened despite detection ambiguity.
        src_cjk = _cjk_ratio(plain_text)
        tgt_cjk = _cjk_ratio(translated_plain)
        if source_lang in ('zh-cn', 'zh') and target_lang == 'en':
            # Chinese -> English: CJK ratio should drop significantly
            if tgt_cjk < src_cjk * 0.5:
                translation_valid = True
        elif source_lang == 'en' and target_lang in ('zh-cn', 'zh'):
            # English -> Chinese: CJK ratio should increase significantly
            if tgt_cjk > src_cjk + 0.1:
                translation_valid = True

    if not translation_valid:
        logger.warning(
            "Translation for %s %d stayed in source language %s, model may have failed",
            item_type, item_id, source_lang,
        )
        return None

    return translated


def process_followup(
    followup: dict,
    ticket_id: int,
    config: AppConfig,
    glpi: GlpiClient,
    ollama: OllamaClient,
    state: ProcessedState,
) -> bool:
    """Process a single followup for translation.

    Returns:
        True if translation was performed
    """
    followup_id = followup.get("id")
    content = followup.get("content", "").strip()

    if not followup_id:
        return False

    # Skip if already processed with same content
    if state.is_followup_processed(followup_id, content):
        return False

    # Skip if already contains translation (content changed after translation, safe to skip)
    if is_already_translated(content, config.translation.prefix):
        state.mark_followup_processed(followup_id, content)
        return False

    # Process translation
    translated = process_text(content, followup_id, "followup", config, ollama)
    if not translated:
        return False  # Don't mark as processed — retry next pass

    # Build new content preserving original
    new_content = build_translated_content(content, translated, config.translation.prefix)

    # Update followup in GLPI
    try:
        glpi.update_followup(ticket_id, followup_id, new_content)
        logger.info("Followup %d translated and updated successfully", followup_id)
        state.mark_followup_processed(followup_id, new_content)
        state.save()
        return True
    except Exception as e:
        logger.error("Failed to update followup %d: %s", followup_id, e)
        return None  # Translation succeeded but API update failed


def process_task(
    task: dict,
    ticket_id: int,
    config: AppConfig,
    glpi: GlpiClient,
    ollama: OllamaClient,
    state: ProcessedState,
) -> bool:
    """Process a single task for translation.

    Returns:
        True if translation was performed
    """
    task_id = task.get("id")
    content = task.get("content", "").strip()

    if not task_id:
        return False

    if state.is_task_processed(task_id, content):
        return False

    if is_already_translated(content, config.translation.prefix):
        state.mark_task_processed(task_id, content)
        return False

    translated = process_text(content, task_id, "task", config, ollama)
    if not translated:
        return False  # Don't mark as processed — retry next pass

    new_content = build_translated_content(content, translated, config.translation.prefix)

    try:
        glpi.update_task(ticket_id, task_id, new_content)
        logger.info("Task %d translated and updated successfully", task_id)
        state.mark_task_processed(task_id, new_content)
        state.save()
        return True
    except Exception as e:
        logger.error("Failed to update task %d: %s", task_id, e)
        return None  # Translation succeeded but API update failed


def process_solution(
    solution: dict,
    ticket_id: int,
    config: AppConfig,
    glpi: GlpiClient,
    ollama: OllamaClient,
    state: ProcessedState,
) -> bool:
    """Process a single solution for translation.

    Returns:
        True if translation was performed
    """
    solution_id = solution.get("id")
    content = solution.get("content", "").strip()

    if not solution_id:
        return False

    if state.is_solution_processed(solution_id, content):
        return False

    if is_already_translated(content, config.translation.prefix):
        state.mark_solution_processed(solution_id, content)
        return False

    translated = process_text(content, solution_id, "solution", config, ollama)
    if not translated:
        return False  # Don't mark as processed — retry next pass

    new_content = build_translated_content(content, translated, config.translation.prefix)

    try:
        glpi.update_solution(ticket_id, solution_id, new_content)
        logger.info("Solution %d translated and updated successfully", solution_id)
        state.mark_solution_processed(solution_id, new_content)
        state.save()
        return True
    except Exception as e:
        logger.error("Failed to update solution %d: %s", solution_id, e)
        return None  # Translation succeeded but API update failed


def process_validation(
    validation: dict,
    ticket_id: int,
    config: AppConfig,
    glpi: GlpiClient,
    ollama: OllamaClient,
    state: ProcessedState,
    existing_followups: list = None,
) -> bool:
    """Process a single validation for translation.

    GLPI validation comments are read-only via the API. Instead of PATCHing
    the validation (which silently fails), we create a followup with the
    translated content. The original validation remains visible on the timeline.

    Translates both submission_comment (approval request) and approval_comment
    (approval answer) if present.

    A hidden marker <!-- vt:ID --> in followup content prevents re-translation
    even when the state file is lost.
    """
    validation_id = validation.get("id")
    if not validation_id:
        return False

    # Check if already translated via marker in existing followups
    marker = f"<!-- vt:{validation_id} -->"
    if existing_followups:
        for fu in existing_followups:
            if marker in fu.get("content", ""):
                if not state.is_submission_processed(validation_id,
                    (validation.get("submission_comment") or "").strip()):
                    state.mark_submission_processed(validation_id,
                        (validation.get("submission_comment") or "").strip())
                    state.save()
                return False

    sub_comment = (validation.get("submission_comment") or "").strip()
    app_comment = (validation.get("approval_comment") or "").strip()

    # Check each comment independently to avoid re-translation when only
    # one changes (e.g. approval added after submission was translated)
    translated_sub = None
    translated_app = None

    if (sub_comment and not is_already_translated(sub_comment, config.translation.prefix)
            and not state.is_submission_processed(validation_id, sub_comment)):
        translated_sub = process_text(sub_comment, validation_id, "validation_request", config, ollama)

    if (app_comment and not is_already_translated(app_comment, config.translation.prefix)
            and not state.is_approval_processed(validation_id, app_comment)):
        translated_app = process_text(app_comment, validation_id, "validation_answer", config, ollama)

    if not translated_sub and not translated_app:
        # Still mark current state to avoid re-checking unchanged items
        if sub_comment:
            state.mark_submission_processed(validation_id, sub_comment)
        if app_comment:
            state.mark_approval_processed(validation_id, app_comment)
        state.save()
        return False

    # Build and post separate followups for request and answer
    posted = False
    if translated_sub:
        sub_content = build_translated_content(
            sub_comment, translated_sub, config.translation.prefix
        ) + f"\n{marker}"
        try:
            glpi.create_followup(ticket_id, sub_content)
            logger.info("Validation %d approval request translation posted", validation_id)
            state.mark_submission_processed(validation_id, sub_comment)
            posted = True
        except Exception as e:
            logger.error("Failed to post validation request translation: %s", e)

    if translated_app:
        app_content = build_translated_content(
            app_comment, translated_app, config.translation.prefix
        ) + f"\n{marker}"
        try:
            glpi.create_followup(ticket_id, app_content)
            logger.info("Validation %d approval answer translation posted", validation_id)
            state.mark_approval_processed(validation_id, app_comment)
            posted = True
        except Exception as e:
            logger.error("Failed to post validation answer translation: %s", e)

    if posted:
        state.save()
        return True

    # Translation was attempted but all followup posts failed
    if translated_sub or translated_app:
        return None

    return False


def process_ticket(
    ticket: dict,
    config: AppConfig,
    glpi: GlpiClient,
    ollama: OllamaClient,
    state: ProcessedState,
) -> bool:
    """Process a ticket's name and content for translation.

    Returns:
        True if any translation was performed
    """
    ticket_id = ticket.get("id")
    if not ticket_id:
        return False

    name = ticket.get("name", "").strip()
    content = ticket.get("content", "").strip()

    # Skip if already processed with same content
    if state.is_ticket_processed(ticket_id, name, content):
        return False

    translated_name = None
    translated_content = None

    # Translate name if needed (skip if already translated with old or new format)
    name_already_translated = is_already_translated(name, config.translation.prefix)
    if not name_already_translated and " / " in name:
        # Check if " / " is a translation separator (old format: "original / translated")
        # vs. a legitimate part of the title (e.g., "Server A / Rack B")
        orig_part, trans_part = name.split(" / ", 1)
        orig_lang = detect_language_with_fallback(
            orig_part.strip(), set(config.translation.source_languages)
        )
        trans_lang = detect_language_with_fallback(
            trans_part.strip(), set(config.translation.source_languages)
        )
        # If the two parts are in different languages, it's likely already translated
        if orig_lang != trans_lang:
            name_already_translated = True
    if name and not name_already_translated:
        translated_name = process_text(name, ticket_id, "ticket_name", config, ollama)

    # Translate content if needed (skip if already translated)
    if content and not is_already_translated(content, config.translation.prefix):
        translated_content = process_text(content, ticket_id, "ticket_content", config, ollama)

    # If nothing to translate, skip without marking — retry next pass
    if not translated_name and not translated_content:
        return False

    # Build update fields
    update_fields = {}
    if translated_name:
        update_fields["name"] = build_translated_title(name, translated_name)
    if translated_content:
        update_fields["content"] = build_translated_content(
            content, translated_content, config.translation.prefix
        )

    # Update ticket in GLPI
    try:
        glpi.update_ticket(ticket_id, **update_fields)
        logger.info("Ticket %d translated and updated successfully", ticket_id)
        # Re-read to get the actual new state
        new_ticket = glpi.get_ticket(ticket_id)
        state.mark_ticket_processed(
            ticket_id,
            new_ticket.get("name", "").strip(),
            new_ticket.get("content", "").strip(),
        )
        state.save()
        return True
    except Exception as e:
        logger.error("Failed to update ticket %d: %s", ticket_id, e)
        return None  # Translation succeeded but API update failed


def run_once(
    config: AppConfig, glpi: GlpiClient, ollama: OllamaClient, state: ProcessedState
) -> dict:
    """Run one translation pass over all tickets.

    Returns:
        Stats dict with counts of translated, skipped, failed items
    """
    stats = {
        "tickets_translated": 0,
        "tickets_skipped": 0,
        "followups_translated": 0,
        "followups_skipped": 0,
        "tasks_translated": 0,
        "tasks_skipped": 0,
        "solutions_translated": 0,
        "solutions_skipped": 0,
        "validations_translated": 0,
        "validations_skipped": 0,
        "failed": 0,
        "tickets_checked": 0,
    }

    try:
        tickets = glpi.get_tickets()
    except Exception as e:
        logger.error("Failed to fetch tickets: %s", e)
        return stats

    stats["tickets_checked"] = len(tickets)
    logger.info("Checking %d tickets...", len(tickets))

    # Collect IDs during main loop for cleanup (avoid redundant API calls)
    all_followup_ids: Set[int] = set()
    all_ticket_ids: Set[int] = set()
    all_task_ids: Set[int] = set()
    all_solution_ids: Set[int] = set()
    all_validation_ids: Set[int] = set()

    for ticket in tickets:
        ticket_id = ticket.get("id")
        if not ticket_id:
            continue

        all_ticket_ids.add(ticket_id)

        # Process ticket name and content
        ticket_result = process_ticket(ticket, config, glpi, ollama, state)
        if ticket_result is True:
            stats["tickets_translated"] += 1
        elif ticket_result is None:
            stats["failed"] += 1
        elif state.is_ticket_processed(
            ticket_id,
            ticket.get("name", "").strip(),
            ticket.get("content", "").strip(),
        ):
            stats["tickets_skipped"] += 1

        # Process followups
        try:
            followups = glpi.get_ticket_followups(ticket_id)
        except Exception as e:
            logger.warning("Failed to fetch followups for ticket %d: %s", ticket_id, e)
            followups = []

        for followup in followups:
            followup_id = followup.get("id", 0)
            if followup_id:
                all_followup_ids.add(followup_id)
            result = process_followup(followup, ticket_id, config, glpi, ollama, state)
            if result is True:
                stats["followups_translated"] += 1
            elif result is None:
                stats["failed"] += 1
            elif followup_id and state.is_followup_processed(followup_id, followup.get("content", "")):
                stats["followups_skipped"] += 1

        # Process tasks
        try:
            tasks = glpi.get_ticket_tasks(ticket_id)
        except Exception as e:
            logger.warning("Failed to fetch tasks for ticket %d: %s", ticket_id, e)
            tasks = []

        for task in tasks:
            task_id = task.get("id", 0)
            if task_id:
                all_task_ids.add(task_id)
            result = process_task(task, ticket_id, config, glpi, ollama, state)
            if result is True:
                stats["tasks_translated"] += 1
            elif result is None:
                stats["failed"] += 1
            elif task_id and state.is_task_processed(task_id, task.get("content", "")):
                stats["tasks_skipped"] += 1

        # Process solutions
        try:
            solutions = glpi.get_ticket_solutions(ticket_id)
        except Exception as e:
            logger.warning("Failed to fetch solutions for ticket %d: %s", ticket_id, e)
            solutions = []

        for solution in solutions:
            solution_id = solution.get("id", 0)
            if solution_id:
                all_solution_ids.add(solution_id)
            result = process_solution(solution, ticket_id, config, glpi, ollama, state)
            if result is True:
                stats["solutions_translated"] += 1
            elif result is None:
                stats["failed"] += 1
            elif solution_id and state.is_solution_processed(solution_id, solution.get("content", "")):
                stats["solutions_skipped"] += 1

        # Process validations
        try:
            validations = glpi.get_ticket_validations(ticket_id)
        except Exception as e:
            logger.warning("Failed to fetch validations for ticket %d: %s", ticket_id, e)
            validations = []

        for validation in validations:
            validation_id = validation.get("id", 0)
            if validation_id:
                all_validation_ids.add(validation_id)
            result = process_validation(validation, ticket_id, config, glpi, ollama, state, followups)
            if result is True:
                stats["validations_translated"] += 1
            elif result is None:
                stats["failed"] += 1
            elif validation_id:
                sub = (validation.get("submission_comment") or "").strip()
                app = (validation.get("approval_comment") or "").strip()
                sub_done = (not sub) or state.is_submission_processed(validation_id, sub)
                app_done = (not app) or state.is_approval_processed(validation_id, app)
                if sub_done and app_done:
                    stats["validations_skipped"] += 1

    # Cleanup stale entries using IDs collected during the main loop
    state.cleanup_followups(all_followup_ids)
    state.cleanup_tickets(all_ticket_ids)
    state.cleanup_tasks(all_task_ids)
    state.cleanup_solutions(all_solution_ids)
    state.cleanup_validations(all_validation_ids)
    state.save()

    return stats


def daemon_loop(config: AppConfig) -> None:
    """Main daemon loop - polls for new items periodically."""
    global _shutdown

    glpi = GlpiClient(config.glpi)
    ollama = OllamaClient(config.ollama)
    state = ProcessedState()

    # Pre-flight checks
    logger.info("=== GLPI Followup Translate starting ===")
    logger.info("GLPI API: %s", config.glpi.api_url)
    logger.info("Ollama API: %s (model: %s)", config.ollama.api_url, config.ollama.model)
    logger.info("Polling interval: %ds", config.polling.interval)

    if not ollama.is_available():
        logger.error("Ollama is not available or model '%s' not found!", config.ollama.model)
        logger.error("Please ensure Ollama is running and the model is pulled.")
        sys.exit(1)

    logger.info("Pre-flight checks passed. Starting polling loop...")

    while not _shutdown:
        try:
            stats = run_once(config, glpi, ollama, state)
            logger.info(
                "Pass complete: %d tickets checked | "
                "Tickets: %d translated, %d skipped | "
                "Followups: %d translated, %d skipped | "
                "Tasks: %d translated, %d skipped | "
                "Solutions: %d translated, %d skipped | "
                "Validations: %d translated, %d skipped | "
                "%d failed",
                stats["tickets_checked"],
                stats["tickets_translated"],
                stats["tickets_skipped"],
                stats["followups_translated"],
                stats["followups_skipped"],
                stats["tasks_translated"],
                stats["tasks_skipped"],
                stats["solutions_translated"],
                stats["solutions_skipped"],
                stats["validations_translated"],
                stats["validations_skipped"],
                stats["failed"],
            )
        except Exception as e:
            logger.error("Error during translation pass: %s", e, exc_info=True)

        # Sleep with interrupt check
        for _ in range(config.polling.interval):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("=== GLPI Followup Translate stopped ===")


def _view_logs(config_path: str = None, lines: int = 50, follow: bool = False) -> None:
    """View recent log entries from the log file.

    Args:
        config_path: Path to config.yaml (to resolve log file path)
        lines: Number of recent lines to display
        follow: If True, continuously tail the log file
    """
    # Fix Windows console encoding for non-ASCII characters
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    # Resolve log file path from config
    try:
        config = load_config(config_path)
        log_file = config.logging.file
    except FileNotFoundError:
        # Fallback: try common log file locations
        for candidate in ["glpi-translate.log", os.path.join(os.getcwd(), "glpi-translate.log")]:
            if os.path.exists(candidate):
                log_file = candidate
                break
        else:
            print("Error: Could not find config or log file. Run the tool first to generate logs.")
            sys.exit(1)

    if not os.path.exists(log_file):
        print(f"Log file not found: {log_file}")
        print("Run the tool first to generate a log file.")
        sys.exit(1)

    if follow:
        # Tail mode: print last lines then follow
        print(f"=== Tailing {log_file} (Ctrl+C to stop) ===\n")
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                # Seek to end and read last N lines
                content = f.readlines()
                for line in content[-lines:]:
                    print(line, end="")
                # Follow new entries
                import time as _time
                while True:
                    line = f.readline()
                    if line:
                        print(line, end="")
                    else:
                        _time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n=== Stopped tailing ===")
    else:
        # Show last N lines
        with open(log_file, "r", encoding="utf-8") as f:
            all_lines = f.readlines()

        display_lines = all_lines[-lines:]
        total = len(all_lines)
        showing = len(display_lines)

        print(f"=== {log_file} (showing last {showing} of {total} lines) ===\n")
        for line in display_lines:
            print(line, end="")
        print()


def _install_service(remove: bool = False) -> None:
    """Install or remove as a background service (systemd / Task Scheduler / launchd)."""
    import platform
    import textwrap
    import shutil

    SYSTEM = platform.system()
    SERVICE_NAME = "glpi-translate"
    WORK_DIR = os.getcwd()
    PYTHON = sys.executable

    if SYSTEM == "Linux":
        # Check root
        if os.geteuid() != 0:
            print("Error: Requires root. Run with sudo:")
            print(f"  sudo {sys.executable} -m glpi_followup_translate --{'remove-' if remove else ''}install-service")
            sys.exit(1)
        unit = textwrap.dedent(f"""\
            [Unit]
            Description=GLPI Followup Translate
            After=network-online.target

            [Service]
            Type=simple
            WorkingDirectory={WORK_DIR}
            ExecStart={PYTHON} -m glpi_followup_translate
            Restart=always
            RestartSec=10
            StandardOutput=append:{WORK_DIR}/glpi-translate.log
            StandardError=append:{WORK_DIR}/glpi-translate.log

            [Install]
            WantedBy=multi-user.target
        """)
        unit_path = f"/etc/systemd/system/{SERVICE_NAME}.service"
        if remove:
            subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)
            subprocess.run(["systemctl", "disable", SERVICE_NAME], check=False)
            os.remove(unit_path)
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            print("Service removed.")
        else:
            with open(unit_path, "w") as f: f.write(unit)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", "--now", SERVICE_NAME], check=True)
            print(f"Service installed. Check: systemctl status {SERVICE_NAME}")

    elif SYSTEM == "Windows":
        xml = textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" version="1.4">
              <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
              <Principals><Principal id="A"><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
              <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
                <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
                <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
                <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
              </Settings>
              <Actions Context="Author"><Exec>
                <Command>{PYTHON}</Command>
                <Arguments>-m glpi_followup_translate</Arguments>
                <WorkingDirectory>{WORK_DIR}</WorkingDirectory>
              </Exec></Actions>
            </Task>
        """)
        xml_path = os.path.join(WORK_DIR, "deploy", "task.xml")
        if remove:
            subprocess.run(["schtasks", "/Delete", "/TN", SERVICE_NAME, "/F"], check=False)
            shutil.rmtree(os.path.dirname(xml_path), ignore_errors=True)
            print("Task removed.")
        else:
            os.makedirs(os.path.dirname(xml_path), exist_ok=True)
            with open(xml_path, "w") as f: f.write(xml)
            try:
                subprocess.run(["schtasks", "/Create", "/TN", SERVICE_NAME, "/XML", xml_path, "/F"],
                              check=True, capture_output=True, text=True)
                subprocess.run(["schtasks", "/Run", "/TN", SERVICE_NAME], check=True)
                print(f"Task installed. Check: schtasks /Query /TN {SERVICE_NAME}")
            except subprocess.CalledProcessError as e:
                if "Access is denied" in (e.stderr or ""):
                    print("Error: Requires Administrator. Run PowerShell as Administrator.")
                    print(f"  glpi-followup-translate --{'remove-' if remove else ''}install-service")
                else:
                    print(f"Error: {e.stderr}")
                sys.exit(1)

    elif SYSTEM == "Darwin":
        plist = textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
              "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0"><dict>
              <key>Label</key><string>com.glpi.translate</string>
              <key>ProgramArguments</key>
              <array><string>{PYTHON}</string><string>-m</string><string>glpi_followup_translate</string></array>
              <key>WorkingDirectory</key><string>{WORK_DIR}</string>
              <key>RunAtLoad</key><true/>
              <key>KeepAlive</key><true/>
              <key>StandardOutPath</key><string>{WORK_DIR}/glpi-translate.log</string>
              <key>StandardErrorPath</key><string>{WORK_DIR}/glpi-translate.log</string>
            </dict></plist>
        """)
        plist_path = os.path.expanduser("~/Library/LaunchAgents/com.glpi.translate.plist")
        if remove:
            subprocess.run(["launchctl", "unload", plist_path], check=False)
            os.remove(plist_path)
            print("Agent removed.")
        else:
            os.makedirs(os.path.dirname(plist_path), exist_ok=True)
            with open(plist_path, "w") as f: f.write(plist)
            subprocess.run(["launchctl", "load", plist_path], check=True)
            print("Agent installed. Check: launchctl list | grep glpi")

    else:
        print(f"Unsupported OS: {SYSTEM}")
        sys.exit(1)


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="GLPI Followup Translate - Auto-translate ticket followups"
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Path to config.yaml (default: auto-detect)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (instead of daemon mode)",
    )
    parser.add_argument(
        "--install-service",
        action="store_true",
        help="Install as background service (systemd/Task Scheduler/launchd)",
    )
    parser.add_argument(
        "--remove-service",
        action="store_true",
        help="Remove background service",
    )
    parser.add_argument(
        "--logs",
        action="store_true",
        help="View recent log entries and exit",
    )
    parser.add_argument(
        "--log-lines",
        type=int,
        default=50,
        help="Number of log lines to display (default: 50)",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Continuously tail the log file (use with --logs)",
    )
    args = parser.parse_args()

    if args.install_service:
        _install_service(remove=False)
        return
    if args.remove_service:
        _install_service(remove=True)
        return
    if args.logs:
        _view_logs(config_path=args.config, lines=args.log_lines, follow=args.follow)
        return

    # Load config
    config = load_config(args.config)

    # Setup logging
    setup_logging(config)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.once:
        glpi = GlpiClient(config.glpi)
        ollama = OllamaClient(config.ollama)
        state = ProcessedState()
        stats = run_once(config, glpi, ollama, state)
        logger.info(
            "Single pass: %d tickets, %d followups, %d tasks, %d solutions, %d validations translated",
            stats["tickets_translated"],
            stats["followups_translated"],
            stats["tasks_translated"],
            stats["solutions_translated"],
            stats["validations_translated"],
        )
    else:
        daemon_loop(config)


if __name__ == "__main__":
    main()
