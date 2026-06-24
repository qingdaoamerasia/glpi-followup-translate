# Changelog

All notable changes to this project are documented in this file.

## [0.4.0] — 2026-06-24

### Added

- **Integrated session cleanup.** GLPI's Symfony framework creates a PHP session file for every API request (even stateless Bearer-token calls). Without cleanup, these files exhaust the filesystem's inodes and crash GLPI. New `glpi.session_dir` config option enables automatic cleanup after each polling cycle. The daemon deletes session files older than `session_max_age` minutes (default: 2x polling interval). Requires write access to GLPI's session directory (root or www-data group).

- **Persistent ticket ID cache.** `ProcessedState` now stores `known_ticket_ids` (all ticket IDs ever discovered) and `highest_probed_id` (highest ID ever probed). This eliminates the full-range downward scan on subsequent polling cycles — the daemon only re-fetches IDs it knows exist, reducing API requests from ~518 to ~119 per cycle (77% reduction).

- **Dynamic upward probe limit.** The upward probe uses a large limit (500) on first run to bridge gaps from service outages, but a small limit (20) on subsequent runs since it starts from `highest_probed_id + 1` instead of re-scanning known-empty ranges. This prevents creating hundreds of GLPI session files per polling cycle.

- **XDG-compliant log directory.** Default log path now follows XDG Base Directory Specification: `~/.local/state/glpi-followup-translate/glpi-translate.log` on Linux. Config and log files are auto-discovered from `~/.config/` and `~/.local/state/` respectively, so the tool works from any directory without `-c` flag.

## [0.3.0] — 2026-06-22

### Fixed

- **GLPI API pagination: only first 100 tickets were translated.** GLPI ignores all Range header variants (plain, items=, query param) and always returns the same 100 tickets regardless of offset. The `_scan_tickets_by_id` fallback only scanned downward from seed max ID, missing any tickets with IDs above the list's max (e.g. 420-467). Added **upward probe** phase that scans from cached/seed max ID + 1 upward, discovering all tickets the list API never returns. Combined with the existing downward scan (now with `MAX_REQUESTS=10000` safety cap instead of the old `max_consecutive_misses=50` that caused premature termination at large deleted-ID gaps), this ensures all tickets are found regardless of GLPI's broken pagination.

- **Language detection misidentified translated content.** `detect_language_with_fallback()` now strips `[AUTO-TRANSLATED]` translation blocks before detection. Previously, if the function was called on already-translated content, the CJK-heavy translation suffix inflated the CJK ratio and caused English-dominant source text to be misidentified as Chinese.

### Added

- **OAuth2 token cross-process persistence.** New `.glpi_token_cache.json` file caches the OAuth2 access/refresh tokens with atomic write (temp file + `os.replace`) and Unix 0600 permissions. Subsequent process starts reuse the cached token instead of requesting a new one, reducing GLPI session accumulation from "one per process start" to "one per hour". Added to `.gitignore`. Can be disabled via `GlpiClient(config, token_cache_file=None)`.

- **Log rotation.** `setup_logging()` now uses `RotatingFileHandler` (5MB max, 3 backups, ~20MB total) instead of unbounded `FileHandler`, preventing disk space exhaustion from long-running daemons.

### Changed

- **Ticket ID scan no longer stops prematurely.** Replaced `max_consecutive_misses=50` threshold with `MAX_REQUESTS=10000` total request safety cap. The old logic broke at contiguous deleted-ID gaps (e.g. tickets 275-326 deleted = 52 consecutive 404s); the new cap allows scanning through any gap size while still bounding total requests.

- **404 log noise reduced.** `_request()` now logs HTTP 404 responses at `DEBUG` level instead of `ERROR`, since 404s are expected during ticket ID scanning (probing deleted IDs). Other HTTP errors (500, 403, etc.) remain at `ERROR`.

- **State file gains `known_max_ticket_id`.** `ProcessedState` persists the highest ticket ID discovered by the ID scan, so subsequent daemon passes start the upward probe from the cached value instead of re-probing all previously discovered IDs.

## [0.2.1] — 2026-06-18

### Fixed

- **stats["failed"] now correctly counts translation failures.** Introduced `TranslationError` exception — `process_text()` raises it when translation fails (timeout, identical output, stayed in source language). All `process_*` functions catch it and return `None`, which `run_once()` counts in the failed stat. Previously, translation failures were silently treated as "skip".
- **CJK language detection no longer misidentifies long English emails as Chinese.** Replaced the aggressive `cjk_count > 0` override with tiered thresholds based on text length: short (<50 chars) needs ≥3 CJK, medium (50–200) needs ≥8 CJK at ≥5% ratio, long (>200) needs ≥30% ratio. A long English email with a few CJK characters in a signature (e.g. "Qingdao Amerasia International") is now correctly detected as English.
- **Long text timeout.** Lowered chunk threshold from 800 to 500 chars so more texts get split into paragraphs. Changed dynamic timeout formula from `len(text)/10` to `len(text)/15` (~15 chars/sec for the 1.8B model).
- **Duplicate log lines.** `setup_logging()` now clears existing handlers before adding new ones, preventing duplicate output when called multiple times (e.g. service restart).

### Added

- `TranslationError` exception class for distinguishing translation failures from "nothing to translate" cases.
- Unit tests for `TranslationError` behavior (4 cases) and `setup_logging` deduplication (2 cases).
- 3 new language detection test cases for tiered CJK threshold edge cases.

## [0.2.0] — 2026-06-16

### Added

- **Placeholder-based glossary enforcement.** Small models (1.8B) tend to translate/romanize CJK terms, breaking post-processing. Source terms are now replaced with `GLS0GLS` placeholders before translation and restored to target terms after, with `_apply_glossary()` as fallback.
- **Full timeline translation.** Added support for tasks, solutions, and validations (submission + approval comments). Validations are read-only — translations are posted as followups with hidden `<!-- vt:ID -->` markers.
- **Unified test suite.** `test_integration.py` combines unit tests (language detection, CJK ratio, glossary, placeholders, output cleanup) and multi-round integration tests against a live GLPI instance.
- **Background service installer.** `--install-service` / `--remove-service` flags for one-command deployment on Linux (systemd), Windows (Task Scheduler), and macOS (launchd).
- **Log viewer.** `--logs` flag to view recent log entries, `--follow` to tail in real-time.

### Fixed

- **is_approval_processed logic.** Fixed asymmetric logic in validation approval tracking to match submission tracking behavior.
- **Cleanup phase redundant API calls.** `run_once()` now reuses IDs collected during the main loop instead of making separate API calls for cleanup.
- **" / " in ticket name false positive.** Title slash separator now uses language detection on both parts — only treated as a translation separator when the two parts are in different languages. Avoids false positives like "Server A / Rack B".

### Changed

- **process_* functions use tri-state return.** `True` = success, `None` = failed (retry next pass), `False` = skipped.
- **Test tickets prefixed with `[Test]`** for easy identification and cleanup in GLPI.

## [0.1.3] — 2026-06-14

### Added

- Cross-platform one-click service installer (`--install-service` / `--remove-service`).
- Permission checks with friendly error messages for service installation.

## [0.1.2] — 2026-06-13

### Changed

- Bumped version (0.1.1 yanked due to PyPI conflict).

## [0.1.1] — 2026-06-13

### Added

- Full ticket timeline translation: followups, tasks, solutions, validations.
- HTML-aware translation with style stripping to reduce token count.
- Dynamic timeout based on text length.
- Chunked translation for long texts (paragraph splitting).
- Content-hash based state tracking (retries on content change).
- CJK character fallback for language detection.

### Fixed

- Multiple translation reliability improvements: output validation, same-language detection, HTML marker formatting.
- Don't mark items as processed when translation fails (enables auto-retry).

[0.3.0]: https://github.com/qingdaoamerasia/glpi-followup-translate/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/qingdaoamerasia/glpi-followup-translate/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/qingdaoamerasia/glpi-followup-translate/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/qingdaoamerasia/glpi-followup-translate/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/qingdaoamerasia/glpi-followup-translate/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/qingdaoamerasia/glpi-followup-translate/releases/tag/v0.1.1
