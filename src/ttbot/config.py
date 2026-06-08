from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str
    target_chat_id: int | None = None
    allowed_chat_ids: str = ""

    # --- Polling ---
    tiktok_profiles: str = "" 
    poll_interval_seconds: int = 300

    # --- Download ---
    provider_order: str = "tikwm,ytdlp"
    tikwm_api_base: str = "https://www.tikwm.com"
    max_file_size_mb: int = 49 
    request_timeout: int = 60
    send_caption: bool = True

    # --- Legacy ---
    download_dir: str = "downloads"
    state_file: str = "state.json"

    @property
    def profiles(self) -> list[str]:
        return [p.strip() for p in self.tiktok_profiles.split(",") if p.strip()]

    @property
    def providers(self) -> list[str]:
        return [p.strip().lower() for p in self.provider_order.split(",") if p.strip()]

    @property
    def allowed_chats(self) -> set[int]:
        out: set[int] = set()
        for raw in self.allowed_chat_ids.split(","):
            raw = raw.strip()
            if raw:
                out.add(int(raw))
        return out
