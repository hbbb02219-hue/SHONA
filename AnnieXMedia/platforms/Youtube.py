import asyncio
import contextlib
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple, Union

import yt_dlp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from youtubesearchpython.__future__ import VideosSearch

from AnnieXMedia.utils.cookie_handler import COOKIE_PATH
from AnnieXMedia.utils.database import is_on_off
from AnnieXMedia.utils.downloader import yt_dlp_download
from AnnieXMedia.utils.errors import capture_internal_err
from AnnieXMedia.utils.formatters import time_to_seconds
from AnnieXMedia.utils.tuning import YTDLP_TIMEOUT, YOUTUBE_META_MAX, YOUTUBE_META_TTL


_cache: Dict[str, Tuple[float, List[Dict]]] = {}
_cache_lock = asyncio.Lock()
_formats_cache: Dict[str, Tuple[float, List[Dict], str]] = {}
_formats_lock = asyncio.Lock()

YOUTUBE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")


def _cookiefile_path() -> Optional[str]:
    path = str(COOKIE_PATH)
    try:
        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    except Exception:
        pass
    return None


def _cookies_args() -> List[str]:
    path = _cookiefile_path()
    return ["--cookies", path] if path else []


async def _exec_proc(*args: str) -> Tuple[bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=YTDLP_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        return b"", b"timeout"


@capture_internal_err
async def cached_youtube_search(query: str) -> List[Dict]:
    key = f"q:{query}"
    now = time.time()

    async with _cache_lock:
        if key in _cache:
            ts, val = _cache[key]
            if now - ts < YOUTUBE_META_TTL:
                return val
            _cache.pop(key, None)

    try:
        data = await VideosSearch(query, limit=1).next()
        result = data.get("result", [])
    except Exception:
        result = []

    if result:
        async with _cache_lock:
            _cache[key] = (now, result)

    return result


class YouTubeAPI:
    def __init__(self) -> None:
        self.base_url = "https://www.youtube.com/watch?v="
        self.playlist_url = "https://youtube.com/playlist?list="
        self._url_pattern = re.compile(r"(?:youtube\.com|youtu\.be)")

    def _prepare_link(self, link: str, videoid=None):
        if isinstance(videoid, str) and videoid:
            return self.base_url + videoid
        link = link.strip()
        if "youtu.be" in link:
            link = self.base_url + link.split("/")[-1]
        return link.split("&")[0]

    @capture_internal_err
    async def track(self, link: str, videoid=None):
        query = self._prepare_link(link, videoid)

        # If not URL, search first
        if not query.startswith("http"):
            results = await cached_youtube_search(query)
            if not results:
                raise ValueError(f"No YouTube results for {query}")
            vid = results[0].get("id")
            query = self.base_url + vid

        stdout, stderr = await _exec_proc("yt-dlp", "--dump-json", query)

        if not stdout:
            raise ValueError(stderr.decode())

        info = json.loads(stdout.decode())

        thumb = (
            info.get("thumbnail")
            or info.get("thumbnails", [{}])[0].get("url", "")
        ).split("?")[0]

        details = {
            "title": info.get("title", ""),
            "link": info.get("webpage_url", query),
            "vidid": info.get("id", ""),
            "duration_min": str(info.get("duration", "")),
            "thumb": thumb,
        }

        return details, info.get("id", "")