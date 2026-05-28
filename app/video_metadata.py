"""Helpers for video metadata documents and API responses."""

import os
import json
import random
import string
import datetime
import logging
from typing import Any, Dict, Optional

from flask import request

from app.database import save_to_db
from app.streaming import build_stream_urls

logger = logging.getLogger(__name__)


def generate_short_id(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


def format_video_document(item: Dict[str, Any]) -> Dict[str, Any]:
    video_id = str(item.get("_id") or item.get("id") or "")
    if not video_id:
        prefix = item.get("storage_prefix", "")
        if prefix.startswith("videos/"):
            video_id = prefix.split("videos/", 1)[1]
    stream_urls = build_stream_urls(video_id) if video_id else {}
    manifest = stream_urls.get("hls_manifest_url") or item.get("hls_manifest_url") or item.get("s3_url", "")
    download = stream_urls.get("download_mp4_url") or item.get("download_mp4_url", "")
    return {
        "id": item.get("_id", ""),
        "short_id": item.get("short_id", ""),
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "s3_url": manifest,
        "hls_manifest_url": manifest,
        "download_mp4_url": download,
        "thumbnail_id": item.get("thumbnail_id", ""),
        "duration": item.get("duration", 0),
        "resolution": item.get("resolution", ""),
        "upload_date": item.get(
            "upload_date", datetime.datetime.now().isoformat()
        ),
        "uploader": item.get("uploader", "Anonymous"),
        "views": item.get("views", 0),
        "likes": item.get("likes", 0),
        "dislikes": item.get("dislikes", 0),
        "players": item.get("players", []),
        "encoding_status": item.get("encoding_status", "ready"),
        "encoding_strategy": item.get("encoding_strategy"),
        "storage_prefix": item.get("storage_prefix", ""),
    }


def combine_and_save_metadata(
    video_metadata: dict,
    form_data: dict,
    internal_name: str,
    thumbnail_id: Optional[str],
    s3_url: str,
    extra_fields: Optional[dict] = None,
) -> dict:
    players = []
    if form_data.get("players"):
        try:
            players = json.loads(form_data.get("players", "[]"))
        except Exception as e:
            logger.error("Error parsing players JSON: %s", e)

    short_id = form_data.get("short_id") or generate_short_id()
    uploader = form_data.get("uploader", "Anonymous")
    uploader_id = None
    uploader_username = None

    if hasattr(request, "current_user") and request.current_user:
        user = request.current_user
        uploader_id = str(user._id)
        uploader_username = user.username
        uploader = f"{user.first_name} {user.last_name}".strip() or user.username

    metadata = {
        "_id": form_data.get("id", internal_name),
        "short_id": short_id,
        "title": form_data.get(
            "title", os.path.basename(video_metadata.get("file_path", ""))
        ),
        "description": form_data.get("description", ""),
        "s3_url": s3_url,
        "internal_name": internal_name,
        "thumbnail_id": thumbnail_id,
        "duration": video_metadata.get("duration", 0),
        "resolution": video_metadata.get("resolution", ""),
        "upload_date": datetime.datetime.now().isoformat(),
        "uploader": uploader,
        "uploader_id": uploader_id,
        "uploader_username": uploader_username,
        "views": 0,
        "likes": 0,
        "dislikes": 0,
        "players": players,
        "encoding_status": "ready",
    }

    if extra_fields:
        metadata.update(extra_fields)

    save_to_db(metadata)
    logger.info("Metadata saved with ID: %s", metadata["_id"])
    return metadata
