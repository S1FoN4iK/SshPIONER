from __future__ import annotations

import asyncio
import logging

from .bot import VideoSender
from .config import Settings
from .state import State
from .tiktok import Downloader, normalize_username

logger = logging.getLogger(__name__)

_FEED_WINDOW = 15 
_BETWEEN_SENDS = 2  


class Poller:
    def __init__(
        self,
        settings: Settings,
        state: State,
        downloader: Downloader,
        sender: VideoSender,
    ) -> None:
        self._settings = settings
        self._state = state
        self._downloader = downloader
        self._sender = sender

    async def run(self) -> None:
        logger.info(
            "poller started: %d profile(s), every %ds -> chat %s",
            len(self._settings.profiles),
            self._settings.poll_interval_seconds,
            self._settings.target_chat_id,
        )
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception: 
                logger.exception("poll tick failed")
            await asyncio.sleep(self._settings.poll_interval_seconds)

    async def _tick(self) -> None:
        target = self._settings.target_chat_id
        if target is None:
            return
        for profile in self._settings.profiles:
            username = normalize_username(profile)
            videos = await self._downloader.list_user(username, _FEED_WINDOW)
            if not videos:
                continue
            ids = [v.video_id for v in videos]

            if not await self._state.is_bootstrapped(username):
                await self._state.seed(username, ids)
                logger.info("bootstrapped @%s with %d existing posts", username, len(ids))
                continue

            fresh = [
                v for v in videos if not await self._state.is_seen(username, v.video_id)
            ]
            for video in reversed(fresh):
                try:
                    await self._sender.send_from_url(target, video.page_url)
                    await self._state.mark_seen(username, video.video_id)
                    logger.info("posted new video %s from @%s", video.video_id, username)
                    await asyncio.sleep(_BETWEEN_SENDS)
                except Exception:
                    logger.exception(
                        "failed to post %s from @%s", video.video_id, username
                    )
