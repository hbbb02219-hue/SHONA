import asyncio
import contextlib
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from youtubesearchpython.__future__ import VideosSearch

from AnnieXMedia.utils.cookie_handler import COOKIE_PATH
from AnnieXMedia.utils.errors import capture_internal_err
from AnnieXMedia.utils.tuning import YTDLP_TIMEOUT, YOUTUBE_META_TTL

# ───────────────────────────
# CACHES
# ───────────────────────────

_cache: Dict[str, Tuple[float, List[Dict]]] = {}
_cache_lock = asyncio.Lock()

YOUTUBE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")


# ───────────────────────────
# COOKIE HANDLER (SAFE)
# ───────────────────────────

def _cookiefile_path() -> Optional[str]:
    try:
        if COOKIE_PATH:
            path = str(COOKIE_PATH)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return path
    except Exception:
        pass
    return None


def _cookies_args() -> List[str]:
    path = _cookiefile_path()
    return ["--cookies", path] if path else []


# ───────────────────────────
# SAFE SUBPROCESS
# ───────────────────────────

async def _exec_proc(*args: str) -> Tuple[bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=YTDLP_TIMEOUT)
        return stdout, stderr
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return b"", b"yt-dlp timeout"


# ───────────────────────────
# YOUTUBE SEARCH CACHE
# ───────────────────────────

@capture_internal_err
async def cached_youtube_search(query: str) -> List[Dict]:
    key = f"q:{query}"
    now = time.time()

    async with _cache_lock:
        if key in _cache:
            ts, data = _cache[key]
            if now - ts < YOUTUBE_META_TTL:
                return data
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


# ───────────────────────────
# MAIN YOUTUBE API
# ───────────────────────────

class YouTubeAPI:
    def __init__(self) -> None:
        self.base_url = "https://www.youtube.com/watch?v="
        self.playlist_url = "https://youtube.com/playlist?list="
        self._url_pattern = re.compile(r"(youtube\.com|youtu\.be)")

    def _prepare_link(self, link: str, videoid: Optional[str] = None) -> str:
        if videoid:
            return self.base_url + videoid

        link = link.strip()

        if "youtu.be" in link:
            link = self.base_url + link.split("/")[-1]

        return link.split("&")[0]

    # ──────────────────────
    # GET SINGLE VIDEO
    # ──────────────────────
    @capture_internal_err
    async def track(self, link: str, videoid: Optional[str] = None):
        query = self._prepare_link(link, videoid)

        # If not a URL → Search
        if not query.startswith("http"):
            results = await cached_youtube_search(query)
            if not results:
                raise ValueError("No YouTube results found")

            vid = results[0].get("id")
            query = self.base_url + vid

        cmd = ["yt-dlp", "--dump-json", query] + _cookies_args()
        stdout, stderr = await _exec_proc(*cmd)

        if not stdout:
            raise RuntimeError(stderr.decode())

        info = json.loads(stdout.decode())

        thumb = (
            info.get("thumbnail")
            or (info.get("thumbnails") or [{}])[0].get("url", "")
        )

        details = {
            "title": info.get("title"),
            "link": info.get("webpage_url"),
            "vidid": info.get("id"),
            "duration": info.get("duration"),
            "thumb": thumb,
        }

        return details, info.get("id")

    # ──────────────────────
    # PLAYLIST
    # ──────────────────────
    @capture_internal_err
    async def playlist(self, url: str):
        cmd = ["yt-dlp", "--dump-single-json", url] + _cookies_args()
        stdout, stderr = await _exec_proc(*cmd)

        if not stdout:
            raise RuntimeError(stderr.decode())

        data = json.loads(stdout.decode())
        videos = []

        for entry in data.get("entries", []):
            videos.append({
                "title": entry.get("title"),
                "id": entry.get("id"),
                "duration": entry.get("duration"),
                "url": entry.get("webpage_url")
            })

        return videos

    # ──────────────────────
    # SEARCH
    # ──────────────────────
    async def search(self, query: str, limit: int = 5):
        data = await VideosSearch(query, limit=limit).next()
        return data.get("result", [])