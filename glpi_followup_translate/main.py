"""Main daemon loop for GLPI Followup Translate."""

import argparse
import json
import logging
import os
import signal
import sys
import time
from typing import Set

from langdetect import detect, LangDetectException

from .config import AppConfig, load_config
from .glpi_client import GlpiClient
from .ollama_client import OllamaClient

logger = logging.getLogger("glpi_followup_translate")

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
    """Track which followups have been processed to avoid re-translation."""

    def __init__(self, state_file: str = "processed_followups.json"):
        self.state_file = state_file
        self.processed: Set[int] = set()
        self._load()

    def _load(self) -> None:
        """Load processed followup IDs from file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    self.processed = set(data.get("processed_ids", []))
                logger.info("Loaded %d processed followup IDs", len(self.processed))
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Failed to load state file, starting fresh: %s", e)
                self.processed = set()

    def save(self) -> None:
        """Save processed followup IDs to file."""
        try:
            with open(self.state_file, "w") as f:
                json.dump({"processed_ids": list(self.processed)}, f)
        except IOError as e:
            logger.error("Failed to save state file: %s", e)

    def is_processed(self, followup_id: int) -> bool:
        return followup_id in self.processed

    def mark_processed(self, followup_id: int) -> None:
        self.processed.add(followup_id)

    def cleanup(self, valid_ids: Set[int]) -> None:
        """Remove IDs that no longer exist in GLPI."""
        before = len(self.processed)
        self.processed &= valid_ids
        removed = before - len(self.processed)
        if removed:
            logger.info("Cleaned up %d stale state entries", removed)


def detect_language(text: str) -> str:
    """Detect the language of the given text.

    Args:
        text: Text to detect language for

    Returns:
        Language code (e.g., 'zh-cn', 'zh', 'en', or 'unknown')
    """
    try:
        lang = detect(text)
        # langdetect returns 'zh-cn', 'zh-tw', 'en', etc.
        return lang.lower()
    except LangDetectException:
        return "unknown"


def is_already_translated(content: str, prefix: str) -> bool:
    """Check if followup content already contains a translation.

    Args:
        content: Followup content
        prefix: Translation prefix marker

    Returns:
        True if content already has translation marker
    """
    return prefix in content


def extract_original_text(content: str, prefix: str) -> str:
    """Extract the original text from a followup that may already have translation.

    Args:
        content: Full followup content
        prefix: Translation prefix marker

    Returns:
        The original (non-translated) portion of the text
    """
    if prefix in content:
        # Split on the first occurrence of prefix and take the part before it
        parts = content.split(prefix, 1)
        return parts[0].strip()
    return content.strip()


def build_translated_content(original: str, translated: str, prefix: str) -> str:
    """Build followup content preserving original and adding translation.

    Args:
        original: Original followup text
        translated: Translated text
        prefix: Translation prefix marker

    Returns:
        Combined content with original preserved and translation appended
    """
    return f"{original}\n\n{prefix}\n{translated}"


def process_followup(
    followup: dict,
    config: AppConfig,
    glpi: GlpiClient,
    ollama: OllamaClient,
    state: ProcessedState,
) -> bool:
    """Process a single followup for translation.

    Args:
        followup: Followup dictionary from GLPI API
        config: Application configuration
        glpi: GLPI API client
        ollama: Ollama API client
        state: Processed state tracker

    Returns:
        True if translation was performed
    """
    followup_id = followup.get("id")
    content = followup.get("content", "").strip()

    if not followup_id or not content:
        return False

    # Skip if already processed
    if state.is_processed(followup_id):
        return False

    # Skip if already translated
    if is_already_translated(content, config.translation.prefix):
        state.mark_processed(followup_id)
        return False

    # Skip if too short
    if len(content) < config.translation.min_text_length:
        state.mark_processed(followup_id)
        return False

    # Detect language
    source_lang = detect_language(content)
    logger.debug("Followup %d detected language: %s", followup_id, source_lang)

    # Check if it's a language we should translate
    target_lang = config.translation.target_language.get(source_lang)
    if not target_lang:
        logger.debug(
            "Followup %d: language '%s' not in translation pairs, skipping",
            followup_id,
            source_lang,
        )
        state.mark_processed(followup_id)
        return False

    # Translate
    logger.info(
        "Translating followup %d (%s -> %s): %s...",
        followup_id,
        source_lang,
        target_lang,
        content[:80],
    )

    translated = ollama.translate(content, source_lang, target_lang)
    if not translated:
        logger.warning("Translation failed for followup %d", followup_id)
        return False

    # Build new content preserving original
    new_content = build_translated_content(
        content, translated, config.translation.prefix
    )

    # Update followup in GLPI
    try:
        glpi.update_followup(followup_id, new_content)
        logger.info("Followup %d translated and updated successfully", followup_id)
        state.mark_processed(followup_id)
        state.save()
        return True
    except Exception as e:
        logger.error("Failed to update followup %d: %s", followup_id, e)
        return False


def run_once(
    config: AppConfig, glpi: GlpiClient, ollama: OllamaClient, state: ProcessedState
) -> dict:
    """Run one translation pass over all tickets.

    Returns:
        Stats dict with counts of translated, skipped, failed followups
    """
    stats = {"translated": 0, "skipped": 0, "failed": 0, "tickets_checked": 0}

    try:
        tickets = glpi.get_tickets()
    except Exception as e:
        logger.error("Failed to fetch tickets: %s", e)
        return stats

    stats["tickets_checked"] = len(tickets)
    logger.info("Checking %d tickets for followups to translate...", len(tickets))

    for ticket in tickets:
        ticket_id = ticket.get("id")
        if not ticket_id:
            continue

        try:
            followups = glpi.get_ticket_followups(ticket_id)
        except Exception as e:
            logger.warning("Failed to fetch followups for ticket %d: %s", ticket_id, e)
            continue

        for followup in followups:
            result = process_followup(followup, config, glpi, ollama, state)
            if result:
                stats["translated"] += 1
            elif not state.is_processed(followup.get("id", 0)):
                stats["skipped"] += 1

    # Periodic cleanup of state (keep only existing followup IDs)
    all_ids: Set[int] = set()
    for ticket in tickets:
        try:
            for fu in glpi.get_ticket_followups(ticket.get("id", 0)):
                if fu.get("id"):
                    all_ids.add(fu["id"])
        except Exception:
            pass
    state.cleanup(all_ids)
    state.save()

    return stats


def daemon_loop(config: AppConfig) -> None:
    """Main daemon loop - polls for new followups periodically."""
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
                "Pass complete: %d tickets checked, %d translated, %d skipped, %d failed",
                stats["tickets_checked"],
                stats["translated"],
                stats["skipped"],
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
        logger.info("Single pass: %d translated, %d skipped", stats["translated"], stats["skipped"])
    else:
        daemon_loop(config)


if __name__ == "__main__":
    main()
