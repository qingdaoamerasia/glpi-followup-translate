# GLPI Followup Translate

Auto-translate GLPI tickets using a local [Ollama](https://ollama.com/) LLM.
Detects Chinese or English content and translates bidirectionally (zh ↔ en).
Works with ticket **names**, **descriptions**, **followups**, **tasks**, **solutions**, and **validations**.

📖 English | [简体中文](README.zh-CN.md)

## Features

- 🔄 **Daemon or one-shot** — polling loop or single-pass mode
- 🌐 **Language detection** — CJK ratio-aware with mixed CN/EN fallback
- 🔀 **Bidirectional** — zh-cn → en, en → zh-cn
- 📖 **Glossary** — domain-specific terminology for consistent translations
- 📝 **Preserves original** — translation appended, never overwritten
- 🎨 **Rich-text aware** — HTML formatting preserved; verbose styles stripped for performance
- 📦 **Full timeline** — followups, tasks, solutions, validations (approval request & answer)
- 🚫 **Dedup** — content-hash state + in-content markers prevent duplicate translations
- 🔄 **Auto-retry** — failed translations retried on next pass
- ✂️ **Chunked translation** — long texts split into paragraphs to avoid timeout
- 📋 **Log viewer** — view recent logs via `--logs` command
- ⚙️ **Configurable** — polling interval, model, language pairs, glossary, min text length
- 💻 **Cross-platform** — Windows, Linux, macOS

## Translation Targets

| Type | Field(s) | Method |
|------|----------|--------|
| **Ticket** | `name`, `content` | PATCH ticket |
| **Followup** | `content` | PATCH followup |
| **Task** | `content` | PATCH task |
| **Solution** | `content` | PATCH solution |
| **Validation** | `submission_comment`, `approval_comment` | Create followup (read-only) |
| **Document** | — | Skipped (no writable content) |

## Translation Format

| Field | Format |
|-------|--------|
| **Title** | `原始标题 / Translated title` |
| **Description** (rich text) | `<p>原始内容</p><br><br><p><strong>[AUTO-TRANSLATED]</strong></p><p>翻译内容</p>` |
| **Description** (plain text) | `原始内容\n\n[AUTO-TRANSLATED]\n翻译内容` |
| **Followup** | Same as description — rich text or plain text depending on content |

### Example — Title

```
服务器无法连接数据库 / The server cannot connect to the database
```

### Example — Rich-Text Description

```html
<p><strong>生产环境</strong>服务器无法连接到
<span style="color: rgb(255, 0, 0);">MySQL数据库</span>。</p>
<br><br>
<p><strong>[AUTO-TRANSLATED]</strong></p>
<p><strong>Production environment</strong> server cannot connect to the
<span style="color: rgb(255, 0, 0);">MySQL database</span>.</p>
```

### Example — Plain-Text Followup

```
检查了防火墙规则，发现3306端口被意外关闭。

[AUTO-TRANSLATED]
Checked the firewall rules and found that port 3306 was accidentally closed.
```

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com/) installed and running
- GLPI instance with API v2.3 and OAuth2 enabled

## Quick Start

### Option A: pip install (recommended)

```bash
# Install from PyPI
pip install glpi-followup-translate

# Pull the translation model
ollama pull kaelri/hy-mt2:1.8b

# Create config in current directory
cp config.yaml.example config.yaml
# Edit config.yaml with your GLPI credentials

# Run
glpi-followup-translate              # daemon mode
glpi-followup-translate --once      # single pass
glpi-followup-translate --logs      # view recent logs
glpi-followup-translate --logs --follow  # tail logs in real-time
glpi-followup-translate -c /path/to/config.yaml  # custom config path
```

### Option B: Development / source install

```bash
# Clone
git clone https://github.com/qingdaoamerasia/glpi-followup-translate.git
cd glpi-followup-translate

# Editable install (recommended for development)
pip install -e .

# Or install dependencies only
pip install -r requirements.txt

# Pull the translation model
ollama pull kaelri/hy-mt2:1.8b

# Configure
cp config.yaml.example config.yaml
# Edit config.yaml with your GLPI credentials

# Run
glpi-followup-translate                 # CLI command
python -m glpi_followup_translate       # or via python module
glpi-followup-translate --once          # single pass
glpi-followup-translate --logs          # view recent logs
```

## Configuration

Copy `config.yaml.example` to `config.yaml` and edit:

```yaml
glpi:
  api_url: "http://your-glpi-server/api.php/v2.3"
  auth_method: "oauth2_password"
  client_id: "your_client_id"
  client_secret: "your_client_secret"
  username: "your_glpi_username"
  password: "your_glpi_password"

ollama:
  api_url: "http://localhost:11434"
  model: "kaelri/hy-mt2:1.8b"
  timeout: 60

polling:
  interval: 60          # seconds between checks

translation:
  prefix: "[AUTO-TRANSLATED]"
  min_text_length: 0    # 0 = translate any length
  source_languages:
    - "zh-cn"
    - "zh"
    - "en"
  target_language:
    zh-cn: "en"
    zh: "en"
    en: "zh-cn"
  glossary:             # domain-specific terms (source_lang: {term: translation})
    zh-cn:
      工单: "ticket"
      数据库: "database"
      服务器: "server"
    en:
      ticket: "工单"
      database: "数据库"
      server: "服务器"

logging:
  level: "INFO"
  file: "glpi-translate.log"
```

| Option | Description | Default |
|--------|-------------|---------|
| `glpi.api_url` | GLPI API endpoint | — |
| `glpi.auth_method` | `oauth2_password` or `app_token` | `oauth2_password` |
| `glpi.client_id` | OAuth2 Client ID | — |
| `glpi.client_secret` | OAuth2 Client Secret | — |
| `glpi.username` | GLPI login username (oauth2_password) | — |
| `glpi.password` | GLPI login password (oauth2_password) | — |
| `ollama.api_url` | Ollama API URL | `http://localhost:11434` |
| `ollama.model` | Translation model | `kaelri/hy-mt2:1.8b` |
| `ollama.timeout` | Request timeout (seconds) | `60` |
| `polling.interval` | Polling interval (seconds) | `60` |
| `translation.prefix` | Translation separator marker | `[AUTO-TRANSLATED]` |
| `translation.min_text_length` | Min plain-text length to translate (0 = no limit) | `0` |
| `translation.source_languages` | Language codes to detect | `["zh-cn", "zh", "en"]` |
| `translation.target_language` | Source→target language mapping | `zh-cn→en, zh→en, en→zh-cn` |
| `translation.glossary` | Per-direction term mappings for consistent translation | `{}` (empty) |
| `logging.level` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `logging.file` | Log file path | `glpi-translate.log` |

## Testing

```bash
# Unit tests only (no GLPI/Ollama required)
python test_integration.py --unit

# Single-ticket quick test (Round 1 only)
python test_integration.py --single

# Run first N rounds
python test_integration.py --rounds 3

# Run ALL rounds (full test suite)
python test_integration.py --rounds 0

# List available test rounds
python test_integration.py --list-rounds

# Clean up test tickets created by this script
python test_integration.py --cleanup
```

### Unit Tests (`--unit`)

Tests language detection, CJK ratio calculation, glossary post-processing,
output cleanup, and placeholder round-trip — all without external services.

### Integration Tests (`--rounds`)

Creates `[Test]`-prefixed tickets on a live GLPI instance, runs a translation
pass, and verifies output format and glossary enforcement. Each round targets
a specific scenario:

| Round | Name | What it tests |
|-------|------|---------------|
| 1 | Rich-text HTML + mixed followups | HTML content, zh/en followup alternation |
| 2 | Short text + long text | Very short strings and multi-paragraph content |
| 3 | Low CJK ratio | Predominantly English text with a few Chinese words |
| 4 | High CJK ratio | Chinese-dominant text with English tech terms |
| 5 | Glossary verification | **Dynamic** — generated from `config.yaml` glossary at runtime |
| 6 | English → Chinese | All-English tickets translated into Chinese |

Round 5 reads glossary terms from `config.yaml` and generates test tickets that
embed those terms, verifying correct translation without hardcoding proprietary
vocabulary in the test file.

## Run 24/7 (Background Service)

One command, auto-detects your OS:

```bash
glpi-followup-translate --install-service     # install
glpi-followup-translate --remove-service      # uninstall
```

| Platform | Service |
|----------|---------|
| Linux | systemd |
| Windows | Task Scheduler |
| macOS | launchd |

## Project Structure

```
glpi-followup-translate/
├── glpi_followup_translate/
│   ├── __init__.py
│   ├── __main__.py         # entry point
│   ├── config.py           # YAML config loader
│   ├── glpi_client.py      # GLPI REST API v2.3 client
│   ├── main.py             # daemon loop, translation logic, log viewer
│   └── ollama_client.py    # Ollama API client
├── config.yaml.example     # config template (safe to commit)
├── pyproject.toml          # pip package configuration
├── requirements.txt
├── test_integration.py     # unified test suite (unit + integration)
├── README.md
├── README.zh-CN.md
└── CLAUDE.md
```

## License

MIT
