from __future__ import annotations

import asyncio
import logging

import httpx
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError

from .bot import VideoSender, build_router
from .config import Settings
from .poller import Poller
from .state import State
from .tiktok import build_downloader

logger = logging.getLogger(__name__)


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = Settings()

    client = httpx.AsyncClient(
        follow_redirects=True, headers={"User-Agent": "tiktok-tg-bot"}
    )
    downloader = build_downloader(settings, client)
    state = State(settings.state_file)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    sender = VideoSender(bot, settings, downloader)
    dp.include_router(build_router(settings, sender))

    poll_task: asyncio.Task | None = None
    if settings.target_chat_id is not None and settings.profiles:
        poll_task = asyncio.create_task(Poller(settings, state, downloader, sender).run())
    else:
        logger.info("polling disabled (need both TARGET_CHAT_ID and TIKTOK_PROFILES)")

    try:
        while True:
            try:
                await dp.start_polling(bot)
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                logger.error("invalid BOT_TOKEN — check your .env")
                raise
            except Exception:
                logger.exception("polling crashed — restarting in 5s")
                await asyncio.sleep(5)
            else:
                break 
    finally:
        if poll_task is not None:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
        await client.aclose()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
