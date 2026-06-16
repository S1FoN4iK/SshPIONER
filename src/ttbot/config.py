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

    # --- Polling (TikTok profiles only) ---
    tiktok_profiles: str = ""
    poll_interval_seconds: int = 300

    # --- Download ---
    # Provider order for TikTok. Instagram/YouTube always go through yt-dlp.
    provider_order: str = "tikwm,ytdlp"
    tikwm_api_base: str = "https://www.tikwm.com"
    max_file_size_mb: int = 49
    request_timeout: int = 60
    send_caption: bool = True
    # Cap YouTube resolution so files stay under the Bot API limit (and skip ffmpeg merges).
    youtube_max_height: int = 720

    # --- Network ---
    # Optional proxy applied to every outbound request (tikwm + yt-dlp).
    # Empty -> direct connection. Examples:
    #   http://user:pass@host:port   socks5://user:pass@host:1080
    proxy_url: str = ""

    # --- Auth (optional, mostly for Instagram / age-gated YouTube) ---
    # Path to a Netscape-format cookies.txt exported from a logged-in browser.
    cookies_file: str = ""
    # Or pull cookies live from an installed browser, e.g. "chrome", "firefox", "edge".
    cookies_from_browser: str = ""

    # --- Storage ---
    state_file: str = "state.db"

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
