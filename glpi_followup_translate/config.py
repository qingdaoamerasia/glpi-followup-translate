"""Configuration loader for GLPI Followup Translate."""

import os
import yaml
from dataclasses import dataclass, field
from typing import Dict, List


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
    file: str = "glpi-translate.log"


@dataclass
class AppConfig:
    glpi: GlpiConfig
    ollama: OllamaConfig
    polling: PollingConfig
    translation: TranslationConfig
    logging: LoggingConfig


def load_config(config_path: str = None) -> AppConfig:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config.yaml. Defaults to same directory as this file.

    Returns:
        AppConfig instance with all settings loaded.
    """
    if config_path is None:
        # Priority 1: config.yaml in current working directory (pip-installed usage)
        cwd_path = os.path.join(os.getcwd(), "config.yaml")
        if os.path.exists(cwd_path):
            config_path = cwd_path
        else:
            # Priority 2: config.yaml in project root (dev mode: python -m glpi_followup_translate)
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config.yaml",
            )

    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Copy config.yaml.example to config.yaml and fill in your values."
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
            file=raw.get("logging", {}).get("file", "glpi-translate.log"),
        ),
    )
