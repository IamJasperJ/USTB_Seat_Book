from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class Settings(BaseModel):
    """Per-account booking preferences, persisted via AccountStore."""

    base_url: str = "https://libzw.ustb.edu.cn"
    login_source: Literal["jwgl", "library"] = "jwgl"
    room_id: int = 102057443
    room_name: str = "图西楼四楼北区"
    target_date: str = "tomorrow"
    seat_names: list[str] = Field(default_factory=lambda: ["F4B114"])
    start_time: str = "07:00"
    end_time: str = "07:40"
    request_timeout_seconds: float = 15
    # Read-only seat status refresh interval used by the web console.
    poll_interval_seconds: float = 30
    auth_session_ttl_seconds: int = 900
    max_browser_sessions: int = Field(default=2, ge=1, le=4)
    session_cleanup_interval_seconds: int = Field(default=30, ge=10, le=300)
    memory_log_interval_seconds: int = Field(default=60, ge=30, le=3600)
    library_navigation_interval_seconds: int = Field(default=60, ge=10, le=600)
    keepalive_enabled: bool = True
    keepalive_interval_seconds: int = 300
    auto_trigger_time: str = "08:00:00"
    auto_retry_interval_seconds: int = 30
    daily_auto_enabled: bool = False
    submit_enabled: bool = False
    headless: bool = True


class GlobalSettings(BaseModel):
    """Deployment-wide infra knobs, loaded once from config.json. Not per-account."""

    base_url: str = "https://libzw.ustb.edu.cn"
    request_timeout_seconds: float = 15
    auth_session_ttl_seconds: int = 900
    max_browser_sessions: int = Field(default=2, ge=1, le=4)
    session_cleanup_interval_seconds: int = Field(default=30, ge=10, le=300)
    memory_log_interval_seconds: int = Field(default=60, ge=30, le=3600)
    library_navigation_interval_seconds: int = Field(default=60, ge=10, le=600)
    headless: bool = True


LEGACY_PERSONAL_FIELDS = (
    "login_source", "room_id", "room_name", "target_date", "seat_names",
    "start_time", "end_time", "poll_interval_seconds", "keepalive_enabled",
    "keepalive_interval_seconds", "auto_trigger_time", "auto_retry_interval_seconds",
    "daily_auto_enabled", "submit_enabled",
)


def load_global_settings(path: str | Path = "config.json") -> GlobalSettings:
    config_path = Path(path)
    if not config_path.exists():
        example = config_path.with_name("config.example.json")
        if example.exists():
            return GlobalSettings.model_validate_json(example.read_text(encoding="utf-8"))
        return GlobalSettings()
    return GlobalSettings.model_validate(json.loads(config_path.read_text(encoding="utf-8")))


def extract_legacy_personal_settings(path: str | Path = "config.json") -> dict[str, Any] | None:
    """Pre-upgrade config.json mixed personal fields into the global file.

    Used once, to seed the first self-registered account with whatever booking
    preferences were already configured, instead of silently discarding them.
    """
    config_path = Path(path)
    if not config_path.exists():
        return None
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found = {key: raw[key] for key in LEGACY_PERSONAL_FIELDS if key in raw}
    return found or None
