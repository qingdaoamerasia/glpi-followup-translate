"""Main daemon loop for GLPI Followup Translate."""

import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
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


# Common HTML tags that are safe to recognize even if not in the original
_SAFE_HTML_TAGS = frozenset({
    'br', 'p', 'strong', 'em', 'b', 'i', 'u', 'a', 'ul', 'ol', 'li',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'tr', 'td', 'th',
    'thead', 'tbody', 'div', 'span', 'img', 'hr', 'pre', 'code',
    'blockquote', 'sup', 'sub', 'dl', 'dt', 'dd',
})

_TAG_RE = re.compile(r'</?([a-zA-Z][a-zA-Z0-9]*)')


def _fix_html_tags(original: str, translated: str) -> str:
    """Fix HTML tag names corrupted by the translation model.

    Small models sometimes alter HTML tag names (e.g. <span> -> <spans>).
    This function compares tags in the translated text against the original
    and corrects unknown tags that closely match a known original tag.

    Only modifies tags that are NOT in the standard safe set AND are close
    variants of a tag present in the original.
    """
    orig_tags = set(_TAG_RE.findall(original))
    if not orig_tags:
        return translated  # original has no HTML, nothing to fix

    trans_tags = set(_TAG_RE.findall(translated))
    unknown = trans_tags - orig_tags - _SAFE_HTML_TAGS

    for bad_tag in unknown:
        # Try to match to a known original tag (e.g. "spans" -> "span")
        for good_tag in orig_tags:
            if bad_tag.startswith(good_tag) and len(bad_tag) - len(good_tag) <= 2:
                translated = re.sub(
                    r'(</?)' + re.escape(bad_tag) + r'(\s|>|/)',
                    r'\g<1>' + good_tag + r'\2',
                    translated,
                )
                break

    return translated

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

    # File handler with rotation (5MB per file, 3 backup files)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(log_level)

    logger.setLevel(log_level)
    # Clear existing handlers to prevent duplicate log lines when called multiple times
    logger.handlers.clear()
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
        # ISO date of last full cleanup ("YYYY-MM-DD")
        self.last_cleanup: str = ""
        # Cached true max ticket ID from the last scan, so the daemon can
        # skip re-probing IDs already discovered in a previous pass.
        self.known_max_ticket_id: int = 0
        # Set of all ticket IDs known to exist, from previous ID scans.
        # Used to skip the full-range downward scan (hundreds of 404s).
        self.known_ticket_ids: set[int] = set()
        # Highest ticket ID ever probed (exists or not). Used to avoid
        # re-probing the same range every polling cycle.
        self.highest_probed_id: int = 0
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
                    self.last_cleanup = data.get("last_cleanup", "")
                    self.known_max_ticket_id = data.get("known_max_ticket_id", 0)
                    self.known_ticket_ids = set(data.get("known_ticket_ids", []))
                    self.highest_probed_id = data.get("highest_probed_id", 0)
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
                        "last_cleanup": self.last_cleanup,
                        "known_max_ticket_id": self.known_max_ticket_id,
                        "known_ticket_ids": sorted(self.known_ticket_ids),
                        "highest_probed_id": self.highest_probed_id,
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

    def needs_cleanup(self) -> bool:
        """Check if a full state cleanup should run today (at most once per day)."""
        today = datetime.now().strftime("%Y-%m-%d")
        return self.last_cleanup != today

    def mark_cleanup_done(self) -> None:
        """Record that a full state cleanup was completed today."""
        self.last_cleanup = datetime.now().strftime("%Y-%m-%d")


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


def _first_sentence(text: str) -> str:
    """Extract the first sentence from text (up to the first sentence terminator)."""
    # Split on common sentence-ending punctuation in both English and Chinese
    for i, c in enumerate(text):
        if c in '。！？.!?':
            return text[:i + 1]
    return text


def detect_language_with_fallback(text: str, supported: set) -> str:
    """Detect language with fallback for short and mixed-language texts.

    langdetect can misidentify short English texts (e.g. "Yes, sure" -> fr),
    short Chinese texts (e.g. "测试" -> ko), and mixed Chinese-English texts.

    Two-tier detection:
    1. Stage 1 (conservative CJK thresholds): catches clearly Chinese text
       regardless of langdetect's verdict.
       - Short (<50 chars): >= 3 CJK chars → zh-cn
       - Medium (50-200 chars): >= 20 CJK chars AND ratio >= 15% → zh-cn
       - Long (>200 chars): ratio >= 30% → zh-cn
    2. Stage 2 (langdetect override): when langdetect says 'en' but CJK
       presence is still significant, override to zh-cn. Uses lower bars
       than Stage 1 to catch bilingual text that langdetect leans English.
       - Medium: >= 8 CJK chars AND ratio >= 14%
       - Long: ratio >= 30%

    This two-tier approach prevents English-dominant text with a few
    scattered CJK vocabulary terms (e.g. "Please check the 数据库 cluster
    and 防火墙 logs") from being misidentified as Chinese.
    """
    # Strip any existing translation block before detection, so the
    # translation suffix does not inflate CJK ratio and mislead detection.
    # [AUTO-TRANSLATED] is the hardcoded marker used throughout the project
    # (config.translation.prefix), so a literal check is safe here.
    _TRANSLATION_MARKER = "[AUTO-TRANSLATED]"
    if _TRANSLATION_MARKER in text:
        text = text.split(_TRANSLATION_MARKER, 1)[0].strip()

    cjk_count = _count_cjk(text)
    ratio = _cjk_ratio(text)
    text_len = len(text)

    # Stage 1: Conservative CJK thresholds — catches clearly Chinese text
    if 'zh-cn' in supported:
        if text_len < 50:
            # Short text: a few CJK chars are a strong signal
            if cjk_count >= 3:
                return 'zh-cn'
        elif text_len <= 200:
            # Medium text: require substantial CJK presence
            if cjk_count >= 20 and ratio >= 0.15:
                return 'zh-cn'
        else:
            # Long text: require substantial CJK presence (30%+)
            if ratio >= 0.3:
                return 'zh-cn'

    # Standard langdetect
    lang = detect_language(text)
    if lang in supported:
        # Stage 2: Override langdetect=en when CJK presence is significant.
        # Uses lower bars than Stage 1 to catch bilingual text where
        # langdetect leans English despite meaningful CJK content.
        if lang == 'en' and 'zh-cn' in supported:
            if (text_len < 50 and cjk_count >= 3) or \
               (50 <= text_len <= 200 and cjk_count >= 8 and ratio >= 0.14) or \
               (text_len > 200 and ratio >= 0.3):
                return 'zh-cn'
        return lang

    # CJK present with langdetect returning unsupported language
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


def _has_cjk(text: str) -> bool:
    """Return True if text contains any CJK character."""
    return any('\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf' for c in text)


def _replace_glossary_term(text: str, src_term: str, replacement: str) -> tuple[str, int]:
    """Replace one glossary term and return (text, replacement_count)."""
    if _has_cjk(src_term):
        count = text.count(src_term)
        return text.replace(src_term, replacement), count

    pattern = r'\b' + re.escape(src_term) + r'\b'
    return re.subn(pattern, replacement, text, flags=re.IGNORECASE)


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
        text, _ = _replace_glossary_term(text, src_term, tgt_term)

    return text


_GLS_PLACEHOLDER_RE = re.compile(r'\[?\s*GLS\s*:\s*(\d+)\s*\]?')


class TranslationError(Exception):
    """Raised when translation fails (timeout, model error, validation failure).

    Distinguished from process_text returning None, which means nothing to
    translate (already translated, wrong language, too short, etc.).
    """
    pass


def _replace_with_placeholders(text: str, glossary: dict) -> tuple:
    """Replace glossary source terms with placeholders before translation.

    Returns:
        (modified_text, mapping) where mapping is {placeholder_id: target_term}

    Uses [GLS:N] bracket format which is more resistant to small translation
    models interpreting the token as translatable text compared to GLS0GLS.
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

        placeholder = f"[GLS:{idx}]"
        text, count = _replace_glossary_term(text, src_term, placeholder)
        if count == 0:
            continue
        mapping[idx] = tgt_term
        idx += 1

    return text, mapping


def _restore_placeholders(text: str, mapping: dict) -> str:
    """Replace placeholders with glossary target terms after translation.

    Small models may add spaces (e.g. '[ GLS : 0 ]'), change case,
    or strip brackets, so we try flexible regex patterns as fallback.
    """
    if not mapping or not text:
        return text

    for idx, tgt_term in mapping.items():
        placeholder = f"[GLS:{idx}]"
        # Try exact match first
        if placeholder in text:
            text = text.replace(placeholder, tgt_term)
        else:
            # Fallback: flexible regex for model artifacts (spaces, case, brackets)
            # Matches: [GLS:0], [ GLS : 0 ], [gls:0], GLS:0, GLS 0, etc.
            pattern = rf'\[?\s*GLS\s*:\s*{idx}\s*\]?'
            text = re.sub(pattern, tgt_term, text, flags=re.IGNORECASE)

    # Fix adjacent English glossary terms concatenated without spaces.
    # This happens when two CJK source terms are adjacent (e.g. "美亚白珊"
    # → "[GLS:0][GLS:1]" → "AmerasiaBaishan" after restoration).
    # Only insert spaces between directly adjacent restored terms, not globally.
    english_targets = {
        idx: t for idx, t in mapping.items()
        if not any('\u4e00' <= c <= '\u9fff' for c in t)
    }
    if len(english_targets) >= 2:
        for i, (_, tgt_a) in enumerate(english_targets.items()):
            for _, tgt_b in english_targets.items():
                if tgt_a != tgt_b:
                    # "AmerasiaBaishan" → "Amerasia Baishan"
                    adjacent = tgt_a + tgt_b
                    spaced = tgt_a + ' ' + tgt_b
                    if adjacent in text:
                        text = text.replace(adjacent, spaced)

    # Remove spaces at CJK-ASCII boundaries introduced by the model or by
    # the adjacent English fix above. In CJK typography there are no spaces
    # between CJK characters and adjacent ASCII word characters.
    # e.g. "Amerasia 系统" → "Amerasia系统", "美亚 Server" → "美亚Server"
    text = re.sub(
        r'([0-9a-zA-Z])\s([\u4e00-\u9fff\u3400-\u4dbf])',
        r'\1\2', text)
    text = re.sub(
        r'([\u4e00-\u9fff\u3400-\u4dbf])\s([0-9a-zA-Z])',
        r'\1\2', text)
    # Also remove spaces between adjacent CJK characters (model artifact
    # from placeholder spacing, e.g. "美亚 系统" → "美亚系统")
    text = re.sub(
        r'([\u4e00-\u9fff\u3400-\u4dbf])\s([\u4e00-\u9fff\u3400-\u4dbf])',
        r'\1\2', text)

    return text


def _translate_chunked(
    text: str, source_lang: str, target_lang: str, ollama: OllamaClient,
    glossary_hint: bool = False,
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
        result = ollama.translate(chunk.strip(), source_lang, target_lang,
                                  glossary_hint=glossary_hint)
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
    has_placeholders = bool(placeholder_mapping)
    if len(compact_text) > 500:
        translated = _translate_chunked(compact_text, source_lang, target_lang, ollama,
                                        glossary_hint=has_placeholders)
    else:
        translated = ollama.translate(compact_text, source_lang, target_lang,
                                      glossary_hint=has_placeholders)
    if not translated:
        raise TranslationError(
            f"Translation failed for {item_type} {item_id} (Ollama returned no result)"
        )

    # Post-process: restore placeholder terms with correct glossary targets
    if placeholder_mapping:
        translated = _restore_placeholders(translated, placeholder_mapping)

    # Post-process: also apply glossary as fallback for any surviving source terms
    if glossary:
        translated = _apply_glossary(translated, glossary)

    # Post-process: fix HTML tags corrupted by the model (e.g. <span> -> <spans>)
    if has_html_tags(text):
        translated = _fix_html_tags(text, translated)

    # Detect truncation: if translation is long but doesn't end with
    # sentence-ending punctuation, the model likely cut off mid-sentence.
    # Retry once to get a complete translation.
    translated_plain = strip_html(translated)
    _SENTENCE_ENDS = '.。!！?？"\'）)>》】;；:：'
    if len(translated_plain) > 80:
        last_char = translated_plain.rstrip()[-1:] if translated_plain.rstrip() else ''
        if last_char and last_char not in _SENTENCE_ENDS and not last_char.isdigit():
            logger.warning(
                "%s %d: translation may be truncated (ends with '%s'), retrying...",
                item_type, item_id, last_char,
            )
            retry = ollama.translate(compact_text, source_lang, target_lang,
                                     glossary_hint=has_placeholders)
            if retry:
                if placeholder_mapping:
                    retry = _restore_placeholders(retry, placeholder_mapping)
                if glossary:
                    retry = _apply_glossary(retry, glossary)
                if has_html_tags(text):
                    retry = _fix_html_tags(text, retry)
                retry_plain = strip_html(retry)
                retry_last = retry_plain.rstrip()[-1:] if retry_plain.rstrip() else ''
                if retry_last in _SENTENCE_ENDS or retry_last.isdigit():
                    logger.info("%s %d: retry produced complete translation", item_type, item_id)
                    translated = retry
                    translated_plain = retry_plain
                else:
                    # Keep longer translation even if both seem truncated
                    if len(retry_plain) > len(translated_plain):
                        translated = retry
                        translated_plain = retry_plain

    # Validate: if the model returned identical text, translation didn't happen.
    # This can occur with small models on short or HTML-preserved input.
    if translated_plain == plain_text:
        raise TranslationError(
            f"Translation for {item_type} {item_id} produced identical text, model may have failed"
        )

    # Validate: first sentence should be translated, not copied from source.
    # The model sometimes echoes the first sentence verbatim while translating
    # the rest, which can pass the overall identical-text and language checks.
    # Skip when the source's first sentence is already in the target language
    # (common in mixed-language texts where English sentences appear in Chinese text).
    if len(plain_text) > 20:
        src_first = _first_sentence(plain_text)
        tgt_first = _first_sentence(translated_plain)
        if len(src_first) > 10 and src_first == tgt_first:
            src_first_cjk = _count_cjk(src_first)
            if target_lang == 'en' and src_first_cjk == 0:
                pass  # Source first sentence already in English, no translation needed
            elif target_lang in ('zh-cn', 'zh') and src_first_cjk > 0:
                pass  # Source first sentence already in Chinese, no translation needed
            else:
                raise TranslationError(
                    f"Translation for {item_type} {item_id} first sentence was not translated: "
                    f"'{src_first[:60]}'"
                )

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
        raise TranslationError(
            f"Translation for {item_type} {item_id} stayed in source language {source_lang}, model may have failed"
        )

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
    try:
        translated = process_text(content, followup_id, "followup", config, ollama)
    except TranslationError as e:
        logger.warning("%s", e)
        return None  # Translation failed — counted as failed by run_once
    if not translated:
        # Nothing to translate — mark as processed if already translated
        if is_already_translated(content, config.translation.prefix):
            state.mark_followup_processed(followup_id, content)
        return False

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

    try:
        translated = process_text(content, task_id, "task", config, ollama)
    except TranslationError as e:
        logger.warning("%s", e)
        return None  # Translation failed — counted as failed by run_once
    if not translated:
        if is_already_translated(content, config.translation.prefix):
            state.mark_task_processed(task_id, content)
        return False

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

    try:
        translated = process_text(content, solution_id, "solution", config, ollama)
    except TranslationError as e:
        logger.warning("%s", e)
        return None  # Translation failed — counted as failed by run_once
    if not translated:
        if is_already_translated(content, config.translation.prefix):
            state.mark_solution_processed(solution_id, content)
        return False

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
    sub_failed = False
    app_failed = False

    if (sub_comment and not is_already_translated(sub_comment, config.translation.prefix)
            and not state.is_submission_processed(validation_id, sub_comment)):
        try:
            translated_sub = process_text(sub_comment, validation_id, "validation_request", config, ollama)
        except TranslationError as e:
            logger.warning("%s", e)
            sub_failed = True

    if (app_comment and not is_already_translated(app_comment, config.translation.prefix)
            and not state.is_approval_processed(validation_id, app_comment)):
        try:
            translated_app = process_text(app_comment, validation_id, "validation_answer", config, ollama)
        except TranslationError as e:
            logger.warning("%s", e)
            app_failed = True

    if not translated_sub and not translated_app:
        # Only mark as processed if nothing failed (skip or already done)
        if not sub_failed and not app_failed:
            if sub_comment:
                state.mark_submission_processed(validation_id, sub_comment)
            if app_comment:
                state.mark_approval_processed(validation_id, app_comment)
            state.save()
        return False if not (sub_failed or app_failed) else None

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
    # (or translation itself failed for the items that had translated content)
    if translated_sub or translated_app or sub_failed or app_failed:
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
    name_failed = False
    content_failed = False

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
        try:
            translated_name = process_text(name, ticket_id, "ticket_name", config, ollama)
        except TranslationError as e:
            logger.warning("%s", e)
            name_failed = True

    # Translate content if needed (skip if already translated)
    if content and not is_already_translated(content, config.translation.prefix):
        try:
            translated_content = process_text(content, ticket_id, "ticket_content", config, ollama)
        except TranslationError as e:
            logger.warning("%s", e)
            content_failed = True

    # If nothing translated (skip, already done, or all failed)
    if not translated_name and not translated_content:
        if name_failed or content_failed:
            return None  # Translation failed — counted as failed by run_once
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
        "tickets_fast_skipped": 0,
        "tickets_full_checked": 0,
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

    # Fetch all tickets by walking the API's 100-item range window.
    try:
        # Sync the persisted true_max_id, known_ticket_ids, and
        # highest_probed_id cache into the client so the ID-scan fallback
        # can skip re-probing already-discovered IDs and deleted-ID gaps.
        glpi._cached_max_ticket_id = state.known_max_ticket_id
        glpi._known_ticket_ids = state.known_ticket_ids
        glpi._highest_probed_id = state.highest_probed_id
        all_tickets = glpi.get_all_tickets(page_size=100)
        # Persist the updated cache back to state for the next run.
        state.known_max_ticket_id = glpi._cached_max_ticket_id
        state.known_ticket_ids = glpi._known_ticket_ids
        state.highest_probed_id = glpi._highest_probed_id
    except Exception as e:
        logger.error("Failed to fetch tickets: %s", e)
        return stats

    # Sort by ID descending client-side (API may not support server-side sort).
    # This ensures newest tickets are processed first for better logging.
    tickets = sorted(all_tickets, key=lambda t: t.get("id", 0), reverse=True)
    stats["tickets_checked"] = len(tickets)
    logger.info("Checking %d tickets...", len(tickets))

    # Determine whether to run a full state cleanup today.
    # On cleanup runs, we re-fetch all sub-items to collect valid IDs.
    # On non-cleanup runs, we skip sub-items for already-processed tickets,
    # which reduces API calls from O(all tickets) to O(new/changed tickets).
    do_cleanup = state.needs_cleanup()
    if do_cleanup:
        logger.info("Running periodic full cleanup pass...")

    # Collect IDs during main loop for cleanup
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

        name = ticket.get("name", "").strip()
        content = ticket.get("content", "").strip()

        # Check state BEFORE processing — if content hash matches, this ticket
        # and all its sub-items were already processed. Skip everything.
        already_done = state.is_ticket_processed(ticket_id, name, content)

        # Process ticket name and content (process_ticket also checks state
        # internally and returns False for already-done tickets)
        ticket_result = process_ticket(ticket, config, glpi, ollama, state)
        if ticket_result is True:
            stats["tickets_translated"] += 1
        elif ticket_result is None:
            stats["failed"] += 1
        elif already_done:
            stats["tickets_skipped"] += 1

        if already_done:
            # Key optimization: skip all sub-item API calls.
            # If the ticket content hasn't changed, all its followups/tasks/
            # solutions/validations were already processed in a previous run.
            stats["tickets_fast_skipped"] += 1
            continue

        stats["tickets_full_checked"] += 1

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

    # Conditional cleanup: only run full cleanup once per day.
    # On cleanup days, re-fetch sub-items for ALL tickets to collect valid IDs,
    # then prune stale state entries (e.g., from deleted GLPI items).
    # On non-cleanup days, state entries accumulate but growth is negligible
    # (~1.6KB/day) and doesn't affect correctness.
    if do_cleanup:
        logger.info("Collecting sub-item IDs for full state cleanup...")
        for ticket in tickets:
            tid = ticket.get("id")
            if not tid:
                continue
            try:
                for fu in glpi.get_ticket_followups(tid):
                    fid = fu.get("id", 0)
                    if fid:
                        all_followup_ids.add(fid)
            except Exception:
                pass
            try:
                for ta in glpi.get_ticket_tasks(tid):
                    taid = ta.get("id", 0)
                    if taid:
                        all_task_ids.add(taid)
            except Exception:
                pass
            try:
                for so in glpi.get_ticket_solutions(tid):
                    sid = so.get("id", 0)
                    if sid:
                        all_solution_ids.add(sid)
            except Exception:
                pass
            try:
                for va in glpi.get_ticket_validations(tid):
                    vid = va.get("id", 0)
                    if vid:
                        all_validation_ids.add(vid)
            except Exception:
                pass

        state.cleanup_followups(all_followup_ids)
        state.cleanup_tickets(all_ticket_ids)
        state.cleanup_tasks(all_task_ids)
        state.cleanup_solutions(all_solution_ids)
        state.cleanup_validations(all_validation_ids)
        state.mark_cleanup_done()
        logger.info("Full state cleanup complete.")

    state.save()

    # Persist OAuth2 token cache so the next process start reuses it
    # instead of requesting a new token (which creates a GLPI session).
    glpi._save_token_cache()

    logger.info(
        "Optimization: %d fast-skipped, %d fully checked (out of %d tickets)",
        stats["tickets_fast_skipped"],
        stats["tickets_full_checked"],
        stats["tickets_checked"],
    )

    return stats


def cleanup_glpi_sessions(config: AppConfig) -> None:
    """Clean stale GLPI PHP session files to prevent inode exhaustion.

    GLPI's Symfony framework creates a PHP session file for every API
    request (even stateless Bearer-token calls). Without cleanup, these
    files accumulate and eventually exhaust the filesystem's inodes.

    This function removes session files older than ``session_max_age``
    minutes from ``session_dir``. It is called after each polling cycle
    when the config has ``glpi.session_dir`` set.

    Requires the daemon to run on the same machine as GLPI.
    """
    session_dir = config.glpi.session_dir
    if not session_dir or not os.path.isdir(session_dir):
        return

    max_age = config.glpi.session_max_age
    if max_age <= 0:
        # Default to 2x polling interval (sessions from a single cycle
        # are used for ~1 second, so 2x is very safe).
        max_age = max(config.polling.interval * 2 // 60, 5)  # at least 5 minutes

    try:
        import glob
        import time

        cutoff = time.time() - (max_age * 60)
        pattern = os.path.join(session_dir, "sess_*")
        removed = 0

        for path in glob.glob(pattern):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    removed += 1
            except OSError:
                continue  # file vanished or permission denied

        if removed > 0:
            logger.info(
                "Session cleanup: removed %d stale file(s) from %s (max age %dm)",
                removed, session_dir, max_age,
            )
    except Exception as e:
        logger.debug("Session cleanup failed: %s", e)


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

        # Clean stale GLPI session files after each pass
        cleanup_glpi_sessions(config)

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
