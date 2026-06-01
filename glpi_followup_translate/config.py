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
        # Default to config.yaml in the project root
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
        ),
        logging=LoggingConfig(
            level=raw.get("logging", {}).get("level", "INFO"),
            file=raw.get("logging", {}).get("file", "glpi-translate.log"),
        ),
    )
