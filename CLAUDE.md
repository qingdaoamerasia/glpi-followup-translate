# CLAUDE.md - GLPI Followup Translate

## Project Overview
Auto-translate GLPI ticket names, descriptions, followups, tasks, solutions, and
validations using local Ollama LLM. Polls GLPI API v2.3, detects language
(Chinese/English), translates via Ollama, and appends the translation. Rich text
(HTML) formatting is preserved.

## Architecture
- **config.py**: YAML config loader with dataclasses
- **glpi_client.py**: GLPI API v2.3 client (OAuth2 Password, full ticket timeline CRUD)
- **ollama_client.py**: Ollama API client (POST /api/generate) with dynamic timeout
- **main.py**: Daemon loop, language detection, state tracking, HTML-aware translation, log viewer

## Translation Targets
All text fields on a ticket timeline:
- **Ticket**: `name` (title), `content` (description)
- **Followup**: `content`
- **Task**: `content`
- **Solution**: `content`
- **Validation**: `submission_comment`, `approval_comment` (read-only — posted as followups)
- **Document**: skipped (no writable text content via API)

## Translation Formats
- **Title**: `original / Translated title` (slash-separated, no prefix marker)
- **Description/Followup (HTML)**: `original\n\n<strong>[AUTO-TRANSLATED]</strong><br>\ntranslated`
- **Description/Followup (plain)**: `original\n\n[AUTO-TRANSLATED]\ntranslated`

## Key Internal Mechanisms

### Language Detection (Tiered CJK Thresholds)
`detect_language_with_fallback()` uses length-aware CJK thresholds:
- Short (<50 chars): >= 3 CJK chars -> zh-cn
- Medium (50-200 chars): >= 8 CJK chars AND ratio >= 5% -> zh-cn
- Long (>200 chars): ratio >= 30% -> zh-cn

This prevents long English emails with a few CJK characters in a signature
from being misidentified as Chinese.

### Glossary (Placeholder Approach)
Small models (1.8B) translate/romanize CJK terms, breaking post-processing.
The solution is a two-phase placeholder approach:
1. `_replace_with_placeholders()`: Replace source terms with `GLS0GLS` tokens before translation
2. `_restore_placeholders()`: Restore target terms after translation
3. `_apply_glossary()`: Fallback find-and-replace for any surviving source terms

### TranslationError Exception
`process_text()` raises `TranslationError` on translation failure (timeout,
identical output, stayed in source language). This distinguishes failures from
"nothing to translate" cases (where it returns `None`).

All `process_*` functions catch `TranslationError` and return `None`, which
`run_once()` counts in `stats["failed"]`.

### Tri-state Return Convention
`process_*` functions return:
- `True` — translation succeeded and API update succeeded
- `None` — translation failed (counted as failed, retried next pass)
- `False` — skipped (already done, nothing to translate)

### Chunked Translation
Texts > 500 chars are split into paragraphs via `_translate_chunked()` to
avoid Ollama timeout. Splits on `\n\n` boundaries, then sentence boundaries
if paragraphs are still > 500 chars.

### Dynamic Timeout
`ollama_client.py` uses `max(config.timeout, len(text) / 15)` — approximately
15 chars/second for the 1.8B model.

## Key APIs
- GLPI OAuth2: `POST {api_url}/token` with `grant_type=password`
- GLPI Tickets: `GET/POST/PUT /Ticket/{id}`
- GLPI Followups: `GET /Ticket/{id}/Timeline/Followup`, `PUT /TicketFollowup/{id}`
- Ollama: `POST /api/generate` with `stream: false`

## Commands
```bash
# Editable install
pip install -e .

# CLI (after pip install)
glpi-followup-translate                  # daemon mode
glpi-followup-translate --once           # single pass
glpi-followup-translate -c config.yaml   # custom config path
glpi-followup-translate --logs           # view recent logs
glpi-followup-translate --logs --follow  # tail logs in real-time
glpi-followup-translate --install-service  # install as background service
glpi-followup-translate --remove-service   # remove background service

# Dev / module
python -m glpi_followup_translate          # daemon mode
python -m glpi_followup_translate --once   # single pass

# Tests
python test_integration.py --unit          # unit tests (no external services)
python test_integration.py                 # multi-ticket integration test
python test_integration.py --single        # single-ticket quick test
python test_integration.py --rounds 0      # run ALL rounds
python test_integration.py --list-rounds   # show available rounds
python test_integration.py --cleanup       # delete test tickets
```

## Config
- `config.yaml` is gitignored (contains secrets)
- `config.yaml.example` is the template
- Config search order (when no `-c` given):
  1. `./config.yaml` in current working directory
  2. `<project_root>/config.yaml` (dev mode fallback)
- `--config` / `-c` CLI flag overrides all defaults

## State
- `processed_state.json` tracks translated IDs with content hashes
- Survives restarts to avoid re-translation
- Stale entries cleaned up each pass using IDs from the current ticket set

## Conventions
- Python 3.9+, type hints everywhere
- logging module (no print)
- requests for HTTP (no aiohttp)
- langdetect for language detection
- Cross-platform \\n usage (Python universal newlines)
- `setup_logging()` clears handlers before adding to prevent duplicates
