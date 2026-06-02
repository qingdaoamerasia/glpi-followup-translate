"""Main daemon loop for GLPI Followup Translate."""

import argparse
import hashlib
import json
import logging
import os
import re
import signal
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
        return old_hash == self._content_hash(app_comment) if app_comment else bool(old_hash)

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


def detect_language_with_fallback(text: str, supported: set) -> str:
    """Detect language with fallback for short and mixed-language texts.

    langdetect can misidentify short English texts (e.g. "Yes, sure" -> fr),
    short Chinese texts (e.g. "测试" -> ko), and mixed Chinese-English texts
    (e.g. "Please check 数据库" -> en).

    Heuristic: if CJK characters are present, treat as zh-cn.
    If only ASCII and short, fall back to en.
    """
    # CJK characters take priority — mixed CN/EN text should be zh-cn -> en
    has_cjk = any('一' <= c <= '鿿' or '㐀' <= c <= '䶿' for c in text)

    if has_cjk and 'zh-cn' in supported:
        return 'zh-cn'

    lang = detect_language(text)
    if lang in supported:
        return lang

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

    For HTML content: wraps [AUTO-TRANSLATED] in <strong> for bold formatting.
    For plain text: uses plain marker with \\n separators.
    """
    if has_html_tags(original):
        return f"{original}\n\n<strong>{prefix}</strong>\n{translated}"
    else:
        return f"{original}\n\n{prefix}\n{translated}"


def build_translated_title(original: str, translated: str) -> str:
    """Build title preserving original and adding translation (slash-separated)."""
    return f"{original} / {translated}"


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
    logger.debug("%s %d detected language: %s", item_type, item_id, source_lang)

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

    # Translate: pass original HTML (with tags) if present, so LLM preserves formatting
    is_html = has_html_tags(text)
    translation_input = text if is_html else plain_text

    logger.info(
        "Translating %s %d (%s -> %s): %s...",
        item_type,
        item_id,
        source_lang,
        target_lang,
        text[:80],
    )

    translated = ollama.translate(translation_input, source_lang, target_lang, preserve_html=is_html)
    if not translated:
        logger.warning("Translation failed for %s %d", item_type, item_id)
        return None

    # Validate: if the model returned identical text, translation didn't happen.
    # This can occur with small models on short or HTML-preserved input.
    translated_plain = strip_html(translated)
    if translated_plain == plain_text:
        logger.warning("Translation for %s %d produced identical text, model may have failed", item_type, item_id)
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
        return False


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
        return False


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
        return False


def process_validation(
    validation: dict,
    ticket_id: int,
    config: AppConfig,
    glpi: GlpiClient,
    ollama: OllamaClient,
    state: ProcessedState,
) -> bool:
    """Process a single validation for translation.

    GLPI validation comments are read-only via the API. Instead of PATCHing
    the validation (which silently fails), we create a followup with the
    translated content. The original validation remains visible on the timeline.

    Translates both submission_comment (approval request) and approval_comment
    (approval answer) if present.

    Returns:
        True if any translation was performed
    """
    validation_id = validation.get("id")
    if not validation_id:
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
        )
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
        )
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
    name_already_translated = is_already_translated(name, config.translation.prefix) or " / " in name
    if name and not name_already_translated:
        translated_name = process_text(name, ticket_id, "ticket_name", config, ollama)

    # Translate content if needed (skip if already translated)
    if content and not is_already_translated(content, config.translation.prefix):
        translated_content = process_text(content, ticket_id, "ticket_content", config, ollama)

    # If nothing to translate, mark current state and skip
    if not translated_name and not translated_content:
        state.mark_ticket_processed(ticket_id, name, content)
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
        return False


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

    for ticket in tickets:
        ticket_id = ticket.get("id")
        if not ticket_id:
            continue

        # Process ticket name and content
        ticket_result = process_ticket(ticket, config, glpi, ollama, state)
        if ticket_result:
            stats["tickets_translated"] += 1
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
            result = process_followup(followup, ticket_id, config, glpi, ollama, state)
            if result:
                stats["followups_translated"] += 1
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
            result = process_task(task, ticket_id, config, glpi, ollama, state)
            if result:
                stats["tasks_translated"] += 1
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
            result = process_solution(solution, ticket_id, config, glpi, ollama, state)
            if result:
                stats["solutions_translated"] += 1
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
            result = process_validation(validation, ticket_id, config, glpi, ollama, state)
            if result:
                stats["validations_translated"] += 1
            elif validation_id:
                sub = (validation.get("submission_comment") or "").strip()
                app = (validation.get("approval_comment") or "").strip()
                sub_done = (not sub) or state.is_submission_processed(validation_id, sub)
                app_done = (not app) or state.is_approval_processed(validation_id, app)
                if sub_done and app_done:
                    stats["validations_skipped"] += 1

    # Periodic cleanup
    all_followup_ids: Set[int] = set()
    all_ticket_ids: Set[int] = set()
    all_task_ids: Set[int] = set()
    all_solution_ids: Set[int] = set()
    all_validation_ids: Set[int] = set()
    for ticket in tickets:
        ticket_id = ticket.get("id")
        if ticket_id:
            all_ticket_ids.add(ticket_id)
            try:
                for fu in glpi.get_ticket_followups(ticket_id):
                    if fu.get("id"):
                        all_followup_ids.add(fu["id"])
            except Exception:
                pass
            try:
                for t in glpi.get_ticket_tasks(ticket_id):
                    if t.get("id"):
                        all_task_ids.add(t["id"])
            except Exception:
                pass
            try:
                for s in glpi.get_ticket_solutions(ticket_id):
                    if s.get("id"):
                        all_solution_ids.add(s["id"])
            except Exception:
                pass
            try:
                for v in glpi.get_ticket_validations(ticket_id):
                    if v.get("id"):
                        all_validation_ids.add(v["id"])
            except Exception:
                pass
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


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="GLPI Followup Translate - Auto-translate ticket followups"
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Path to config.yaml (default: auto-detect in project root)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (instead of daemon mode)",
    )
    args = parser.parse_args()

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
