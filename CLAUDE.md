# CLAUDE.md - GLPI Followup Translate

## Project Overview
Auto-translate GLPI ticket followups using local Ollama LLM. Polls GLPI API v2.3 for new followups, detects language (Chinese/English), translates via Ollama, and updates the followup with translated text appended.

## Architecture
- **config.py**: YAML config loader with dataclasses
- **glpi_client.py**: GLPI API v2.3 client (OAuth2 Client Credentials, followup CRUD)
- **ollama_client.py**: Ollama API client for translation (POST /api/generate)
- **main.py**: Daemon loop with language detection, state tracking, graceful shutdown

## Key APIs
- GLPI OAuth2: `POST {api_url}/token` with `grant_type=client_credentials`
- GLPI Followups: `GET /Ticket/{id}/TicketFollowup`, `PUT /TicketFollowup/{id}`
- Ollama: `POST /api/generate` with `stream: false`

## Commands
```bash
pip install -r requirements.txt
python -m glpi_followup_translate          # daemon mode
python -m glpi_followup_translate --once   # single pass
```

## Config
- `config.yaml` is gitignored (contains secrets)
- `config.yaml.example` is the template
- Config is loaded from project root by default

## State
- `processed_followups.json` tracks which followup IDs have been translated
- Survives restarts to avoid re-translation

## Conventions
- Python 3.9+, type hints everywhere
- logging module (no print)
- requests for HTTP (no aiohttp)
- langdetect for language detection
