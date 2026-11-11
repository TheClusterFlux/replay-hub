"""Admission limits shared by upload handlers and the encoding worker."""
import json
import math
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, ServiceUnavailable

MIB = 1024 * 1024
CLIENT_LIMIT = int(os.getenv("BROWSER_ENCODING_MAX_MIB", "64")) * MIB
SERVER_LIMIT = int(os.getenv("SERVER_UPLOAD_MAX_MIB", "512")) * MIB
RESERVE = int(os.getenv("ENCODING_DISK_RESERVE_MIB", "512")) * MIB
MAX_SECONDS = int(os.getenv("ENCODING_MAX_MINUTES", "30")) * 60
MAX_HEIGHT = int(os.getenv("ENCODING_OUTPUT_HEIGHT", "720"))
QUEUE_LIMIT = max(1, int(os.getenv("SERVER_UPLOAD_SLOTS", "2")))
MAX_CHUNK = 8 * MIB
# Container demuxers only. A file named .mp4 must not open a playlist or another URL.
INPUT_OPTIONS = ["-protocol_whitelist", "file", "-format_whitelist",
                 "mov,matroska,webm,avi,asf,flv,mpeg,mpegts,ogg"]


def check_space(directory, needed):
    if shutil.disk_usage(directory).free < needed + RESERVE:
        raise ServiceUnavailable("The server has too little temporary disk space. Try a smaller clip or retry later.")


def check_capacity(size, directory):
    if not isinstance(size, int) or size <= 0:
        raise BadRequest("Choose a non-empty video file.")
    if SERVER_LIMIT <= 0 or not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ServiceUnavailable("Server encoding is unavailable. Try a smaller clip that your browser can encode.")
    if size > SERVER_LIMIT:
        raise RequestEntityTooLarge(f"The server accepts up to {SERVER_LIMIT // MIB} MiB per video. Trim or compress this file.")
    from app.storage import check_write_storage
    check_write_storage()
    check_space(directory, size * 3)
    check_memory()


def check_memory():
    try:
        limit = int(Path("/sys/fs/cgroup/memory.max").read_text().strip())
        used = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
        # Linux charges cached upload pages to the cgroup. Inactive pages are reclaimable.
        stats = dict(line.split() for line in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines())
        if limit - used + int(stats.get("inactive_file", 0)) < 96 * MIB:
            raise ServiceUnavailable("The server is low on memory. Retry later or upload a smaller clip.")
    except (ValueError, OSError):
        pass


def probe_source(path):
    cmd = ["ffprobe", "-v", "error", *INPUT_OPTIONS, "-select_streams", "v:0",
           "-show_entries", "format=duration:stream=width,height,avg_frame_rate", "-of", "json", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, check=True, timeout=25)
        info = json.loads(result.stdout)
        stream = info["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(info["format"]["duration"])
        rate = float(Fraction(stream.get("avg_frame_rate", "0")))
        if not math.isfinite(duration) or duration <= 0 or min(width, height) <= 0:
            raise ValueError()
    except (OSError, subprocess.SubprocessError, KeyError, IndexError, ValueError, ZeroDivisionError) as exc:
        raise BadRequest("The server could not read this video. Export it as an MP4 or WebM and try again.") from exc
    if duration > MAX_SECONDS or max(width, height) > 1920 or min(width, height) > 1080 or rate > 60.1:
        raise RequestEntityTooLarge(f"Server encoding supports up to 1080p, 60 fps and {MAX_SECONDS // 60} minutes. Trim or downscale this video.")
    return {"duration": duration, "resolution": f"{width}x{height}", "file_path": str(path)}


def scratch_bytes(size, duration):
    # Original + downloadable MP4 + the bitrate ladder with muxing headroom.
    bitrate = 4_800_000
    if MAX_HEIGHT < 720:
        bitrate = 1_300_000
    elif MAX_HEIGHT >= 1080:
        bitrate = 10_000_000
    return 2 * size + math.ceil(duration * bitrate / 8)
