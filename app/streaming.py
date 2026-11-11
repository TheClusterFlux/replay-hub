"""Stable VOD playlist URLs with on-demand segment signing."""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from app.config import API_PUBLIC_BASE_URL
from app.storage import get_object_text, video_storage_prefix

logger = logging.getLogger(__name__)


def storage_is_ready() -> bool:
    from app.config import R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, USE_R2

    return bool(USE_R2 and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY)


def build_stream_urls(video_id: str) -> Dict[str, str]:
    base = API_PUBLIC_BASE_URL.rstrip("/")
    prefix = video_storage_prefix(video_id)
    return {
        "storage_prefix": prefix,
        "hls_manifest_url": f"{base}/stream/{quote(video_id, safe='')}/master.m3u8",
        "download_mp4_url": f"{base}/stream/{quote(video_id, safe='')}/download",
        "s3_url": f"{base}/stream/{quote(video_id, safe='')}/master.m3u8",
        "poster_url": f"{base}/stream/{quote(video_id, safe='')}/poster.jpg",
    }


def stream_url_for_video(item: Dict[str, Any], path: str) -> str:
    video_id = item.get("_id", "")
    base = API_PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/stream/{quote(video_id, safe='')}/{path.lstrip('/')}"


def resolve_object_key(video_id: str, rel_path: str) -> Optional[str]:
    rel = rel_path.lstrip("/")
    if (not re.fullmatch(r"[A-Za-z0-9_-]+", video_id)
            or not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)*", rel)
            or ".." in rel.split("/")):
        return None
    return f"{video_storage_prefix(video_id)}/{rel}"


def rewrite_master_playlist(text: str, video_id: str, base: str = API_PUBLIC_BASE_URL) -> str:
    base = base.rstrip("/")
    out: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            rel = stripped.lstrip("/")
            out.append(f"{base}/stream/{quote(video_id, safe='')}/{rel}")
        else:
            out.append(line)
    body = "\n".join(out)
    return body if body.endswith("\n") else body + "\n"


def fetch_playlist_text(object_key: str) -> Optional[str]:
    return get_object_text(object_key)
