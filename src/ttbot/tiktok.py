from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TIKTOK_URL_RE = re.compile(r"https?://(?:[\w-]+\.)?tiktok\.com/[^\s]+", re.IGNORECASE)


@dataclass
class TikTokVideo:
    video_id: str
    author: str
    description: str
    page_url: str
    direct_url: str | None = None  
    images: list[str] | None = None  
    music_url: str | None = None 
    width: int | None = None
    height: int | None = None
    duration: float | None = None

    @property
    def is_photo(self) -> bool:
        return bool(self.images)


@dataclass
class DownloadResult:
    video: TikTokVideo
    provider: str
    video_path: Path | None = None 
    image_paths: list[Path] = field(default_factory=list) 
    audio_path: Path | None = None  

    @property
    def is_photo(self) -> bool:
        return self.video_path is None and bool(self.image_paths)


class ProviderError(RuntimeError):
    pass


class DownloadError(RuntimeError):
    pass


def find_tiktok_url(text: str | None) -> str | None:
    if not text:
        return None
    m = TIKTOK_URL_RE.search(text)
    return m.group(0) if m else None


def looks_like_profile_url(url: str) -> bool:
    return bool(re.search(r"tiktok\.com/@[\w.\-]+/?$", url, re.IGNORECASE))


def normalize_username(profile: str) -> str:
    p = profile.strip()
    if p.startswith("http"):
        m = re.search(r"@([\w.\-]+)", p)
        p = m.group(1) if m else p.rstrip("/").split("/")[-1]
    return p.lstrip("@")


def page_url_for(username: str, video_id: str) -> str:
    return f"https://www.tiktok.com/@{username}/video/{video_id}"


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class TikwmProvider:
    """tikwm.com — explicit watermark-free stream (HD) and photo-post images. Primary source."""

    name = "tikwm"

    def __init__(self, base: str, client: httpx.AsyncClient, timeout: int) -> None:
        self._base = base.rstrip("/")
        self._client = client
        self._timeout = timeout

    def _absolute(self, url: str) -> str:
        return f"{self._base}{url}" if url.startswith("/") else url

    async def resolve(self, url: str) -> TikTokVideo:
        resp = await self._client.get(
            f"{self._base}/api/",
            params={"url": url, "hd": 1},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0 or not payload.get("data"):
            raise ProviderError(f"tikwm: {payload.get('msg', 'bad response')}")
        d = payload["data"]

        images = d.get("images") or None
        music = None
        if images:
            music = d.get("music") or (d.get("music_info") or {}).get("play")
            direct = None
        else:
            direct = d.get("hdplay") or d.get("play") or d.get("wmplay")
            if not direct:
                raise ProviderError("tikwm: no playable url in response")

        author = (d.get("author") or {}).get("unique_id") or ""
        return TikTokVideo(
            video_id=str(d.get("id") or ""),
            author=author,
            description=d.get("title") or "",
            page_url=url,
            direct_url=self._absolute(direct) if direct else None,
            images=[self._absolute(u) for u in images] if images else None,
            music_url=self._absolute(music) if music else None,
            duration=_as_float(d.get("duration")),
        )

    async def list_user(self, username: str, count: int) -> list[TikTokVideo]:
        resp = await self._client.get(
            f"{self._base}/api/user/posts",
            params={"unique_id": username, "count": count},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise ProviderError(f"tikwm user/posts: {payload.get('msg', 'bad response')}")
        items = (payload.get("data") or {}).get("videos") or []
        videos: list[TikTokVideo] = []
        for it in items:
            vid = str(it.get("video_id") or it.get("id") or "")
            if not vid:
                continue
            videos.append(
                TikTokVideo(
                    video_id=vid,
                    author=username,
                    description=it.get("title") or "",
                    page_url=page_url_for(username, vid),
                    duration=_as_float(it.get("duration")),
                )
            )
        return videos

    async def _stream_to(self, url: str, dest: Path) -> None:
        headers = {"User-Agent": _UA, "Referer": "https://www.tiktok.com/"}
        async with self._client.stream(
            "GET", url, headers=headers, timeout=self._timeout
        ) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    fh.write(chunk)

    async def download(self, video: TikTokVideo, dest_dir: Path) -> DownloadResult:
        if video.direct_url is None and not video.images:
            video = await self.resolve(video.page_url)

        if video.images:
            return await self._download_photo(video, dest_dir)

        assert video.direct_url
        dest = dest_dir / f"{video.video_id or 'video'}_{self.name}.mp4"
        await self._stream_to(video.direct_url, dest)
        return DownloadResult(video=video, provider=self.name, video_path=dest)

    async def _download_photo(self, video: TikTokVideo, dest_dir: Path) -> DownloadResult:
        image_paths: list[Path] = []
        for i, img_url in enumerate(video.images or []):
            dest = dest_dir / f"{video.video_id or 'photo'}_{i:02d}.jpg"
            try:
                await self._stream_to(img_url, dest)
                image_paths.append(dest)
            except Exception as exc: 
                logger.warning("tikwm: image %d download failed: %s", i, exc)

        audio_path: Path | None = None
        if video.music_url:
            a = dest_dir / f"{video.video_id or 'photo'}.mp3"
            try:
                await self._stream_to(video.music_url, a)
                audio_path = a
            except Exception as exc:
                logger.warning("tikwm: music download failed: %s", exc)

        return DownloadResult(
            video=video,
            provider=self.name,
            image_paths=image_paths,
            audio_path=audio_path,
        )


class YtDlpProvider:
    """yt-dlp — self-contained fallback, no third-party service involved. Video posts only."""

    name = "ytdlp"

    def __init__(self, timeout: int, proxy: str | None = None) -> None:
        self._timeout = timeout
        self._proxy = proxy or None

    def _base_opts(self) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": self._timeout,
        }
        if self._proxy:
            opts["proxy"] = self._proxy
        return opts

    def _extract(self, url: str, download: bool, outtmpl: str | None = None) -> dict:
        import yt_dlp

        opts = self._base_opts()
        opts["skip_download"] = not download
        if download:
            opts["outtmpl"] = outtmpl
            opts["format"] = "best"
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download)

    def _extract_flat(self, url: str, count: int) -> dict:
        import yt_dlp

        opts = self._base_opts()
        opts.pop("noplaylist", None)
        opts["extract_flat"] = True
        opts["playlistend"] = count
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    async def resolve(self, url: str) -> TikTokVideo:
        info = await asyncio.to_thread(self._extract, url, False)
        return _video_from_ytdlp(info, url)

    async def list_user(self, username: str, count: int) -> list[TikTokVideo]:
        url = f"https://www.tiktok.com/@{username}"
        info = await asyncio.to_thread(self._extract_flat, url, count)
        out: list[TikTokVideo] = []
        for e in (info.get("entries") or [])[:count]:
            vid = str(e.get("id") or "")
            if not vid:
                continue
            out.append(
                TikTokVideo(
                    video_id=vid,
                    author=username,
                    description=e.get("title") or "",
                    page_url=e.get("url") or page_url_for(username, vid),
                )
            )
        return out

    async def download(self, video: TikTokVideo, dest_dir: Path) -> DownloadResult:
        outtmpl = str(dest_dir / f"{video.video_id or 'video'}_{self.name}.%(ext)s")
        info = await asyncio.to_thread(self._extract, video.page_url, True, outtmpl)
        downloads = info.get("requested_downloads") or []
        path: Path | None = None
        if downloads and downloads[0].get("filepath"):
            p = Path(downloads[0]["filepath"])
            if p.exists():
                path = p
        if path is None:
            matches = sorted(dest_dir.glob(f"{video.video_id or 'video'}_{self.name}.*"))
            if not matches:
                raise ProviderError("ytdlp: output file not found")
            path = matches[0]
        return DownloadResult(video=video, provider=self.name, video_path=path)


def _video_from_ytdlp(info: dict, url: str) -> TikTokVideo:
    return TikTokVideo(
        video_id=str(info.get("id") or ""),
        author=info.get("uploader_id") or info.get("uploader") or "",
        description=info.get("description") or info.get("title") or "",
        page_url=info.get("webpage_url") or url,
        width=info.get("width"),
        height=info.get("height"),
        duration=_as_float(info.get("duration")),
    )


class Downloader:
    def __init__(self, providers: list) -> None:
        if not providers:
            raise ValueError("no providers configured")
        self._providers = providers

    @staticmethod
    def _ensure_nonempty(result: DownloadResult) -> None:
        if result.video_path is not None:
            p = result.video_path
            if not p.exists() or p.stat().st_size == 0:
                raise ProviderError("downloaded an empty file")
        elif result.image_paths:
            usable = [p for p in result.image_paths if p.exists() and p.stat().st_size > 0]
            if not usable:
                raise ProviderError("downloaded no usable images")
            result.image_paths = usable
        else:
            raise ProviderError("nothing was downloaded")

    async def download(self, url: str, dest_dir: Path) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for provider in self._providers:
            try:
                video = await provider.resolve(url)
                result = await provider.download(video, dest_dir)
                self._ensure_nonempty(result)
                kind = "photo" if result.is_photo else "video"
                logger.info("downloaded %s (%s) via %s", url, kind, provider.name)
                return result
            except Exception as exc: 
                last = exc
                logger.warning("provider %s failed for %s: %s", provider.name, url, exc)
        raise DownloadError(f"all providers failed for {url}: {last}")

    async def list_user(self, username: str, count: int) -> list[TikTokVideo]:
        last: Exception | None = None
        for provider in self._providers:
            try:
                videos = await provider.list_user(username, count)
                if videos:
                    return videos
            except Exception as exc: 
                last = exc
                logger.warning(
                    "provider %s list_user failed for %s: %s", provider.name, username, exc
                )
        if last:
            logger.warning("list_user: all providers failed for %s: %s", username, last)
        return []


def build_downloader(settings, client: httpx.AsyncClient) -> Downloader:
    proxy = getattr(settings, "proxy_url", "") or None
    providers: list = []
    for name in settings.providers:
        if name == "tikwm":
            providers.append(
                TikwmProvider(settings.tikwm_api_base, client, settings.request_timeout)
            )
        elif name == "ytdlp":
            providers.append(YtDlpProvider(settings.request_timeout, proxy))
        else:
            logger.warning("unknown provider %r - skipped", name)
    if not providers:
        providers = [
            TikwmProvider(settings.tikwm_api_base, client, settings.request_timeout),
            YtDlpProvider(settings.request_timeout, proxy),
        ]
    return Downloader(providers)