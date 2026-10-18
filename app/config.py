"""Настройки из переменных окружения (на Render задаются в Environment)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "да")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v and v.strip() else default


def _list(name: str) -> list[str]:
    return [x.strip() for x in _env(name).split(",") if x.strip()]


def _database_url() -> str:
    url = _env("DATABASE_URL", "sqlite:///./local.db")
    # Render отдаёт postgres:// или postgresql:// — SQLAlchemy нужен драйвер psycopg 3
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    # Магазин (подставляется в системный промпт вместо {{...}})
    store_name: str = field(default_factory=lambda: _env("STORE_NAME", "NUR Electronics"))
    store_instagram: str = field(default_factory=lambda: _env("STORE_INSTAGRAM", "@nurelectronics.kz"))
    store_address: str = field(default_factory=lambda: _env("STORE_ADDRESS", "Астана, ул. Төле би, 52"))
    store_hours: str = field(default_factory=lambda: _env("STORE_HOURS", "ежедневно с 10:00 до 20:00"))
    store_whatsapp: str = field(default_factory=lambda: _env("STORE_WHATSAPP"))  # например https://wa.me/7707...

    # Instagram / Meta
    ig_verify_token: str = field(default_factory=lambda: _env("IG_VERIFY_TOKEN"))
    ig_app_secret: str = field(default_factory=lambda: _env("IG_APP_SECRET"))
    ig_access_token: str = field(default_factory=lambda: _env("IG_ACCESS_TOKEN"))
    ig_account_id: str = field(default_factory=lambda: _env("IG_ACCOUNT_ID"))
    graph_api_version: str = field(default_factory=lambda: _env("GRAPH_API_VERSION", "v23.0"))

    # Claude
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    max_tokens: int = field(default_factory=lambda: _int("MAX_TOKENS", 1200))
    web_search_enabled: bool = field(default_factory=lambda: _bool("WEB_SEARCH_ENABLED", True))
    web_search_tool_type: str = field(default_factory=lambda: _env("WEB_SEARCH_TOOL_TYPE", "web_search_20250305"))
    web_search_domains: list[str] = field(
        default_factory=lambda: _list("WEB_SEARCH_DOMAINS") or ["apple.com", "support.apple.com"])

    # Google Sheets
    spreadsheet_id: str = field(default_factory=lambda: _env("SPREADSHEET_ID"))
    google_service_account_json: str = field(default_factory=lambda: _env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    google_api_key: str = field(default_factory=lambda: _env("GOOGLE_API_KEY"))
    tradein_sheet: str = field(default_factory=lambda: _env("TRADEIN_SHEET", "Закуп б/у"))
    catalog_cache_seconds: int = field(default_factory=lambda: _int("CATALOG_CACHE_SECONDS", 300))
    price_file: str = field(default_factory=lambda: _env("PRICE_FILE"))  # офлайн-режим для разработки

    # Хранилище
    database_url: str = field(default_factory=_database_url)

    # Поведение
    bot_enabled: bool = field(default_factory=lambda: _bool("BOT_ENABLED", True))
    allowed_user_ids: list[str] = field(default_factory=lambda: _list("ALLOWED_USER_IDS"))
    debounce_seconds: float = field(default_factory=lambda: float(_env("DEBOUNCE_SECONDS", "4")))
    history_limit: int = field(default_factory=lambda: _int("HISTORY_LIMIT", 30))
    manager_pause_minutes: int = field(default_factory=lambda: _int("MANAGER_PAUSE_MINUTES", 5))
    resume_after_pause: bool = field(default_factory=lambda: _bool("RESUME_AFTER_PAUSE", True))
    echo_grace_seconds: float = field(default_factory=lambda: float(_env("ECHO_GRACE_SECONDS", "3")))

    followup_enabled: bool = field(default_factory=lambda: _bool("FOLLOWUP_ENABLED", False))
    followup_after_minutes: int = field(default_factory=lambda: _int("FOLLOWUP_AFTER_MINUTES", 180))
    window_hours: int = field(default_factory=lambda: _int("WINDOW_HOURS", 23))
    work_hour_start: int = field(default_factory=lambda: _int("WORK_HOUR_START", 10))
    work_hour_end: int = field(default_factory=lambda: _int("WORK_HOUR_END", 20))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Asia/Almaty"))

    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN"))
    system_prompt_path: str = field(
        default_factory=lambda: _env("SYSTEM_PROMPT_PATH", str(ROOT / "prompts" / "system_prompt.md")))


settings = Settings()
