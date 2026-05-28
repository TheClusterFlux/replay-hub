"""Server-side HLS encoding with ffmpeg (480p / 720p / 1080p ladder)."""

import os
import json
import shutil
import logging
import subprocess
from typing import Callable, Dict, Optional

from app.config import HLS_SEGMENT_SECONDS, ENCODING_FFMPEG_THREADS

logger = logging.getLogger(__name__)

BITRATE_LADDER = [
    ("480p", 480, "1000k", "128k"),
    ("720p", 720, "2500k", "128k"),
    ("1080p", 1080, "5000k", "192k"),
]


def check_ffmpeg_available() -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_ffmpeg(cmd: list, timeout: int = 7200) -> None:
    logger.info("ffmpeg: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "ffmpeg failed")


def _probe_height(input_path: str) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        input_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return 1080
    data = json.loads(result.stdout)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            return int(stream.get("height") or 1080)
    return 1080


def _write_master_playlist(output_dir: str, variants: list) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for name, bandwidth in variants:
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}")
        lines.append(f"{name}/index.m3u8")
    master_path = os.path.join(output_dir, "master.m3u8")
    with open(master_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return master_path


def encode_to_hls(
    input_path: str,
    output_dir: str,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, str]:
    """
    Encode input video to HLS ladder + poster + original MP4 copy.

    Returns dict with keys: output_dir, master_playlist, original_mp4, poster
    """
    if not check_ffmpeg_available():
        raise RuntimeError("ffmpeg is not available on this system")

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    source_height = _probe_height(input_path)
    applicable = [r for r in BITRATE_LADDER if r[1] <= source_height]
    if not applicable:
        applicable = [BITRATE_LADDER[0]]

    threads = str(ENCODING_FFMPEG_THREADS)
    variants_for_master = []
    total = len(applicable) + 2  # poster + original copy

    for idx, (name, height, vbitrate, abitrate) in enumerate(applicable):
        if on_progress:
            on_progress(f"Encoding {name}", idx + 1, total)

        variant_dir = os.path.join(output_dir, name)
        os.makedirs(variant_dir, exist_ok=True)
        segment_pattern = os.path.join(variant_dir, "seg_%05d.ts")
        playlist_path = os.path.join(variant_dir, "index.m3u8")

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-vf",
            f"scale=-2:{height}",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-threads",
            threads,
            "-b:v",
            vbitrate,
            "-maxrate",
            vbitrate,
            "-bufsize",
            str(int(vbitrate.replace("k", "")) * 2) + "k",
            "-c:a",
            "aac",
            "-b:a",
            abitrate,
            "-ac",
            "2",
            "-hls_time",
            str(HLS_SEGMENT_SECONDS),
            "-hls_playlist_type",
            "vod",
            "-hls_flags",
            "independent_segments",
            "-hls_segment_filename",
            segment_pattern,
            playlist_path,
        ]
        _run_ffmpeg(cmd)
        bw = int(vbitrate.replace("k", "")) * 1000 + int(abitrate.replace("k", "")) * 1000
        variants_for_master.append((name, bw))

    if on_progress:
        on_progress("Creating poster", len(applicable) + 1, total)

    poster_path = os.path.join(output_dir, "poster.jpg")
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "1",
            "-i",
            input_path,
            "-vframes",
            "1",
            "-q:v",
            "2",
            poster_path,
        ],
        timeout=120,
    )

    if on_progress:
        on_progress("Preserving original MP4", len(applicable) + 2, total)

    original_path = os.path.join(output_dir, "original.mp4")
    shutil.copy2(input_path, original_path)

    master_path = _write_master_playlist(output_dir, variants_for_master)

    return {
        "output_dir": output_dir,
        "master_playlist": master_path,
        "original_mp4": original_path,
        "poster": poster_path,
    }
