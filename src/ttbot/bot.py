from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from .config import Settings
from .tiktok import (
    Downloader,
    DownloadResult,
    TikTokVideo,
    find_tiktok_url,
    looks_like_profile_url,
)

logger = logging.getLogger(__name__)


class VideoSender:
    def __init__(self, bot, settings: Settings, downloader: Downloader) -> None:
        self._bot = bot
        self._settings = settings
        self._downloader = downloader

    def _caption(self, video: TikTokVideo) -> str | None:
        if not self._settings.send_caption:
            return None
        parts: list[str] = []
        if video.author:
            parts.append(f"@{video.author}")
        if video.page_url:
            parts.append(video.page_url)
        return "\n".join(parts) or None

    async def send_from_url(
        self, chat_id: int, url: str, reply_to: int | None = None
    ) -> bool:
        with tempfile.TemporaryDirectory(prefix="tt_") as tmp:
            result = await self._downloader.download(url, Path(tmp))
            if result.is_photo:
                return await self._send_photos(chat_id, result, reply_to)
            return await self._send_video(chat_id, result, reply_to)

    async def _send_video(
        self, chat_id: int, result: DownloadResult, reply_to: int | None
    ) -> bool:
        assert result.video_path is not None
        size_mb = result.video_path.stat().st_size / (1024 * 1024)
        if size_mb > self._settings.max_file_size_mb:
            link = result.video.direct_url or result.video.page_url
            await self._bot.send_message(
                chat_id,
                f"Видео весит {size_mb:.0f} МБ — больше лимита Bot API "
                f"({self._settings.max_file_size_mb} МБ).\n"
                f"Без вотермарки можно забрать тут: {link}",
                reply_to_message_id=reply_to,
            )
            return False
        v = result.video
        await self._bot.send_video(
            chat_id,
            video=FSInputFile(result.video_path),
            caption=self._caption(v),
            width=v.width,
            height=v.height,
            duration=int(v.duration) if v.duration else None,
            supports_streaming=True,
            reply_to_message_id=reply_to,
        )
        return True

    async def _send_photos(
        self, chat_id: int, result: DownloadResult, reply_to: int | None
    ) -> bool:
        v = result.video
        caption = self._caption(v)
        paths = result.image_paths
        sent = False
        idx = 0
        first = True
        while idx < len(paths):
            batch = paths[idx : idx + 10]
            rt = reply_to if first else None
            if len(batch) == 1:
                await self._bot.send_photo(
                    chat_id,
                    photo=FSInputFile(batch[0]),
                    caption=caption if first else None,
                    reply_to_message_id=rt,
                )
            else:
                media = [
                    InputMediaPhoto(
                        media=FSInputFile(p),
                        caption=(caption if (first and i == 0) else None),
                    )
                    for i, p in enumerate(batch)
                ]
                await self._bot.send_media_group(
                    chat_id, media=media, reply_to_message_id=rt
                )
            sent = True
            first = False
            idx += len(batch)
            await asyncio.sleep(1) 

        if result.audio_path is not None:
            try:
                await self._bot.send_audio(
                    chat_id,
                    audio=FSInputFile(result.audio_path),
                    title=(v.description[:60] if v.description else None),
                    performer=(v.author or None),
                )
            except Exception as exc: 
                logger.warning("failed to send audio for %s: %s", v.page_url, exc)
        return sent


def build_router(settings: Settings, sender: VideoSender) -> Router:
    router = Router()

    allowed = settings.allowed_chats
    if allowed:
        router.message.filter(F.chat.id.in_(allowed))

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(
            "Дай ссылку на ТикТок — пришлю видео без вотермарки."
        )

    @router.message(F.text | F.caption)
    async def on_message(message: Message) -> None:
        url = find_tiktok_url(message.text or message.caption)
        if not url:
            return
        if looks_like_profile_url(url):
            await message.reply(
                "Это ссылка на профиль, добавить новые профили можно в .env"
            )
            return

        status = await message.reply("⬇️ Скачиваем…")
        try:
            await sender.send_from_url(
                message.chat.id, url, reply_to=message.message_id
            )
            try:
                await status.delete()
            except Exception:
                pass
        except Exception as exc: 
            logger.exception("failed to handle %s", url)
            try:
                await status.edit_text(f"Не получилось скачать: {exc}")
            except Exception: 
                pass

    return router