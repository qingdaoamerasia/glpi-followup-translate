"""Configuration loader for GLPI Followup Translate."""

import os
import sys
import yaml
from dataclasses import dataclass, field
from typing import Dict, List

_APP_NAME = "glpi-followup-translate"
_LOG_FILENAME = "glpi-translate.log"


def default_log_dir() -> str:
    """Return the XDG-compliant log directory for this application.

    Follows the XDG Base Directory Specification:
    - Linux:   $XDG_STATE_HOME/glpi-followup-translate/  (~/.local/state/...)
    - macOS:   ~/Library/Logs/glpi-followup-translate/
    - Windows: %LOCALAPPDATA%\\glpi-followup-translate\\Logs\\
    """
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Logs", _APP_NAME)
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
        return os.path.join(local, _APP_NAME, "Logs")
    else:
        # Linux / other Unix — XDG_STATE_HOME
        state_home = os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state"))
        return os.path.join(state_home, _APP_NAME)


def default_log_path() -> str:
    """Return the full default log file path."""
    return os.path.join(default_log_dir(), _LOG_FILENAME)


def resolve_log_file(log_file: str, config_path: str = None) -> str:
    """Resolve a log file path to an absolute path.

    Empty string → XDG default.  Relative path → resolved against config
    directory (or cwd if no config path given).
    """
    if not log_file:
        return default_log_path()
    if os.path.isabs(log_file):
        return log_file
    config_dir = os.path.dirname(os.path.abspath(
        config_path or os.path.join(os.getcwd(), "config.yaml")
    ))
    return os.path.join(config_dir, log_file)


@dataclass
class GlpiConfig:
    api_url: str
    client_id: str
    client_secret: str
    username: str = ""
    password: str = ""
    auth_method: str = "oauth2_password"  # "oauth2_password" or "app_token"
    # Path to GLPI's PHP session directory on the local filesystem.
    # When set, the daemon cleans stale session files after each polling
    # cycle to prevent inode exhaustion. Only works when the daemon runs
    # on the same machine as GLPI. Leave empty to disable.
    session_dir: str = ""
    # Maximum age (in minutes) for session files before cleanup.
    # Defaults to 2x polling interval. Only used when session_dir is set.
    session_max_age: int = 0


@dataclass
class OllamaConfig:
    api_url: str = "http://localhost:11434"
    model: str = "kaelri/hy-mt2:1.8b"
    timeout: int = 60


@dataclass
class PollingConfig:
    interval: int = 60


@dataclass
class TranslationConfig:
    prefix: str = "[AUTO-TRANSLATED]"
    min_text_length: int = 10
    source_languages: List[str] = field(default_factory=lambda: ["zh-cn", "zh", "en"])
    target_language: Dict[str, str] = field(
        default_factory=lambda: {"zh-cn": "en", "zh": "en", "en": "zh-cn"}
    )
    glossary: Dict[str, Dict[str, str]] = field(default_factory=dict)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    # Log file path. Empty = XDG default (see default_log_dir()).
    file: str = ""


@dataclass
class AppConfig:
    glpi: GlpiConfig
    ollama: OllamaConfig
    polling: PollingConfig
    translation: TranslationConfig
    logging: LoggingConfig


def load_config(config_path: str = None) -> AppConfig:
    """Load configuration from YAML file.

    Search order when config_path is None:
    1. ./config.yaml  (current working directory)
    2. ~/.config/glpi-followup-translate/config.yaml  (XDG standard)
    3. <project_root>/config.yaml  (dev mode)

    Args:
        config_path: Path to config.yaml. Defaults to auto-detect.

    Returns:
        AppConfig instance with all settings loaded.
    """
    if config_path is None:
        # Priority 1: config.yaml in current working directory
        cwd_path = os.path.join(os.getcwd(), "config.yaml")
        if os.path.exists(cwd_path):
            config_path = cwd_path
        else:
            # Priority 2: XDG standard user config location
            xdg_path = os.path.join(
                os.path.expanduser("~"),
                ".config", "glpi-followup-translate", "config.yaml",
            )
            if os.path.exists(xdg_path):
                config_path = xdg_path
            else:
                # Priority 3: config.yaml in project root (dev mode)
                config_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config.yaml",
                )

    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Searched in:\n"
            "  1. ./config.yaml  (current directory)\n"
            "  2. ~/.config/glpi-followup-translate/config.yaml  (XDG standard)\n"
            "  3. <project_root>/config.yaml  (dev mode)\n"
            "\n"
            "Fix: copy config.yaml.example to one of these locations,\n"
            "or use -c /path/to/config.yaml to specify the path."
        )

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return AppConfig(
        glpi=GlpiConfig(
            api_url=raw["glpi"]["api_url"],
            client_id=raw["glpi"]["client_id"],
            client_secret=raw["glpi"]["client_secret"],
            username=raw["glpi"].get("username", ""),
            password=raw["glpi"].get("password", ""),
            auth_method=raw["glpi"].get("auth_method", "oauth2_password"),
            session_dir=raw["glpi"].get("session_dir", ""),
            session_max_age=int(raw["glpi"].get("session_max_age", 0)),
        ),
        ollama=OllamaConfig(
            api_url=raw.get("ollama", {}).get("api_url", "http://localhost:11434"),
            model=raw.get("ollama", {}).get("model", "kaelri/hy-mt2:1.8b"),
            timeout=raw.get("ollama", {}).get("timeout", 60),
        ),
        polling=PollingConfig(
            interval=raw.get("polling", {}).get("interval", 60),
        ),
        translation=TranslationConfig(
            prefix=raw.get("translation", {}).get("prefix", "[AUTO-TRANSLATED]"),
            min_text_length=raw.get("translation", {}).get("min_text_length", 10),
            source_languages=raw.get("translation", {}).get(
                "source_languages", ["zh-cn", "zh", "en"]
            ),
            target_language=raw.get("translation", {}).get(
                "target_language", {"zh-cn": "en", "zh": "en", "en": "zh-cn"}
            ),
            glossary={
                lang: {str(k): str(v) for k, v in terms.items()}
                for lang, terms in raw.get("translation", {}).get("glossary", {}).items()
                if isinstance(terms, dict)
            },
        ),
        logging=LoggingConfig(
            level=raw.get("logging", {}).get("level", "INFO"),
            file=raw.get("logging", {}).get("file", ""),
        ),
    )
