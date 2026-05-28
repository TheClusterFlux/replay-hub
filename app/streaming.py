"""Presigned HLS streaming with sliding-window playlist signing."""

import logging
import posixpath
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from app.config import (
    API_PUBLIC_BASE_URL,
    STREAM_LOOKBEHIND_SECONDS,
    STREAM_PRESIGN_EXPIRY,
    STREAM_WINDOW_SECONDS,
)
from app.storage import generate_presigned_get, get_object_text, video_storage_prefix

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
    if not rel or ".." in rel.split("/"):
        return None
    return f"{video_storage_prefix(video_id)}/{rel}"


def parse_media_segments(text: str) -> Tuple[List[Dict[str, Any]], float]:
    segments: List[Dict[str, Any]] = []
    pending_duration: Optional[float] = None
    cursor = 0.0
    index = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            match = re.match(r"#EXTINF:([0-9.]+)", line)
            pending_duration = float(match.group(1)) if match else 0.0
            continue
        if line.startswith("#"):
            continue
        if pending_duration is None:
            continue
        segments.append(
            {
                "duration": pending_duration,
                "uri": line,
                "index": index,
                "start_time": cursor,
            }
        )
        cursor += pending_duration
        index += 1
        pending_duration = None

    return segments, cursor


def rewrite_master_playlist(text: str, video_id: str) -> str:
    base = API_PUBLIC_BASE_URL.rstrip("/")
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


def rewrite_variant_playlist(
    text: str,
    segment_key_prefix: str,
    playback_time: float,
) -> str:
    segments, _total = parse_media_segments(text)
    if not segments:
        return text

    window_start = max(0.0, playback_time - STREAM_LOOKBEHIND_SECONDS)
    window_end = window_start + STREAM_WINDOW_SECONDS

    selected = [
        seg
        for seg in segments
        if (seg["start_time"] + seg["duration"]) > window_start
        and seg["start_time"] < window_end
    ]
    if not selected:
        selected = [segments[0]]

    target_duration = max(int(max(seg["duration"] for seg in selected)) + 1, 1)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-MEDIA-SEQUENCE:{selected[0]['index']}",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
    ]

    for seg in selected:
        object_key = f"{segment_key_prefix}/{seg['uri']}".replace("//", "/")
        signed = generate_presigned_get(object_key, expires_in=STREAM_PRESIGN_EXPIRY)
        if not signed:
            raise RuntimeError(f"failed to presign segment {object_key}")
        lines.append(f"#EXTINF:{seg['duration']:.3f},")
        lines.append(signed)

    if selected[-1]["index"] >= segments[-1]["index"]:
        lines.append("#EXT-X-ENDLIST")

    return "\n".join(lines) + "\n"


def fetch_playlist_text(object_key: str) -> Optional[str]:
    return get_object_text(object_key)


def segment_prefix_for_playlist(video_id: str, rel_path: str) -> str:
    playlist_dir = posixpath.dirname(rel_path)
    if playlist_dir:
        return f"{video_storage_prefix(video_id)}/{playlist_dir}"
    return video_storage_prefix(video_id)
