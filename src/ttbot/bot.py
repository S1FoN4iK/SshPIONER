from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from .config import Settings
from .media import (
    Downloader,
    DownloadResult,
    Media,
    find_media_url,
    looks_like_profile_url,
)

logger = logging.getLogger(__name__)


class VideoSender:
    def __init__(self, bot, settings: Settings, downloader: Downloader) -> None:
        self._bot = bot
        self._settings = settings
        self._downloader = downloader

    @property
    def _size_limit(self) -> int:
        return self._settings.max_file_size_mb

    def _caption(self, media: Media) -> str | None:
        if not self._settings.send_caption:
            return None
        parts: list[str] = []
        if media.author:
            parts.append(f"@{media.author}")
        if media.page_url:
            parts.append(media.page_url)
        return "\n".join(parts) or None

    @staticmethod
    def _size_mb(path: Path) -> float:
        return path.stat().st_size / (1024 * 1024)

    async def send_from_url(
        self, chat_id: int, url: str, reply_to: int | None = None
    ) -> bool:
        with tempfile.TemporaryDirectory(prefix="dl_") as tmp:
            result = await self._downloader.download(url, Path(tmp))
            return await self._send_result(chat_id, result, reply_to)

    async def _send_result(
        self, chat_id: int, result: DownloadResult, reply_to: int | None
    ) -> bool:
        if result.is_single_video:
            return await self._send_single_video(chat_id, result, reply_to)
        sent = await self._send_album(chat_id, result, reply_to)
        await self._send_audio(chat_id, result)
        return sent

    async def _send_single_video(
        self, chat_id: int, result: DownloadResult, reply_to: int | None
    ) -> bool:
        media = result.media
        path = result.videos[0]
        size_mb = self._size_mb(path)
        if size_mb > self._size_limit:
            link = media.direct_url or media.page_url
            await self._bot.send_message(
                chat_id,
                f"Файл весит {size_mb:.0f} МБ — больше лимита Bot API "
                f"({self._size_limit} МБ).\nЗабрать оригинал: {link}",
                reply_to_message_id=reply_to,
            )
            return False
        await self._bot.send_video(
            chat_id,
            video=FSInputFile(path),
            caption=self._caption(media),
            width=media.width,
            height=media.height,
            duration=int(media.duration) if media.duration else None,
            supports_streaming=True,
            reply_to_message_id=reply_to,
        )
        return True

    def _build_album(
        self, result: DownloadResult, caption: str | None
    ) -> tuple[list, int]:

        entries: list[tuple[Path, bool]] = [] 
        skipped = 0
        for path in result.videos:
            if self._size_mb(path) > self._size_limit:
                skipped += 1
                continue
            entries.append((path, True))
        for path in result.images:
            entries.append((path, False))

        items: list = []
        for i, (path, is_video) in enumerate(entries):
            cap = caption if i == 0 else None
            if is_video:
                items.append(
                    InputMediaVideo(
                        media=FSInputFile(path), caption=cap, supports_streaming=True
                    )
                )
            else:
                items.append(InputMediaPhoto(media=FSInputFile(path), caption=cap))
        return items, skipped

    async def _send_album(
        self, chat_id: int, result: DownloadResult, reply_to: int | None
    ) -> bool:
        caption = self._caption(result.media)
        items, skipped = self._build_album(result, caption)

        sent = False
        first = True
        for i in range(0, len(items), 10):
            batch = items[i : i + 10]
            rt = reply_to if first else None
            if len(batch) == 1:
                await self._send_one(chat_id, batch[0], rt)
            else:
                await self._bot.send_media_group(chat_id, media=batch, reply_to_message_id=rt)
            sent = True
            first = False
            if i + 10 < len(items):
                await asyncio.sleep(1)

        if skipped:
            await self._bot.send_message(
                chat_id,
                f"{skipped} файл(ов) превысили лимит {self._size_limit} МБ "
                f"и не отправлены. Оригинал: {result.media.page_url}",
                reply_to_message_id=reply_to if not sent else None,
            )
            sent = True
        return sent

    async def _send_one(self, chat_id: int, item, reply_to: int | None) -> None:
        if isinstance(item, InputMediaVideo):
            await self._bot.send_video(
                chat_id,
                video=item.media,
                caption=item.caption,
                supports_streaming=True,
                reply_to_message_id=reply_to,
            )
        else:
            await self._bot.send_photo(
                chat_id,
                photo=item.media,
                caption=item.caption,
                reply_to_message_id=reply_to,
            )

    async def _send_audio(self, chat_id: int, result: DownloadResult) -> None:
        if result.audio is None:
            return
        media = result.media
        try:
            await self._bot.send_audio(
                chat_id,
                audio=FSInputFile(result.audio),
                title=(media.description[:60] if media.description else None),
                performer=(media.author or None),
            )
        except Exception as exc:
            logger.warning("failed to send audio for %s: %s", media.page_url, exc)


def build_router(settings: Settings, sender: VideoSender) -> Router:
    router = Router()

    allowed = settings.allowed_chats
    if allowed:
        router.message.filter(F.chat.id.in_(allowed))

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(
            "Киньте ссылку на TikTok, Instagram (reel/post/фото) или YouTube — "
            "получите результат без вотермарки."
        )

    @router.message(F.text | F.caption)
    async def on_message(message: Message) -> None:
        url = find_media_url(message.text or message.caption)
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
