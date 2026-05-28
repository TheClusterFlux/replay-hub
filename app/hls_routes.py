"""HLS upload API routes (client presign + complete, config)."""

import json
import uuid
import logging

from flask import request, jsonify

from app import app
from app.auth import jwt_required
from app.config import VIDEO_ENCODING_STRATEGY, R2_BUCKET_NAME
from app.streaming import storage_is_ready
from app.storage import (
    generate_presigned_put,
    get_content_type,
    object_exists,
    video_storage_prefix,
    build_video_urls,
)
from app.database import get_single_document, update_db
from app.video_metadata import (
    format_video_document,
    combine_and_save_metadata,
    generate_short_id,
)
from app.encoding_queue import get_encoding_status

logger = logging.getLogger(__name__)


@app.route("/api/config", methods=["GET"])
def get_public_config():
    return jsonify(
        {
            "encoding_strategy": VIDEO_ENCODING_STRATEGY,
            "r2_bucket": R2_BUCKET_NAME,
            "storage_ready": storage_is_ready(),
        }
    ), 200


@app.route("/api/videos/<video_id>/encoding-status", methods=["GET"])
def encoding_status(video_id):
    return jsonify(get_encoding_status(video_id)), 200


@app.route("/upload/hls/presign", methods=["POST"])
@jwt_required
def hls_presign():
    data = request.get_json(silent=True) or {}
    video_id = data.get("video_id") or str(uuid.uuid4())
    files = data.get("files", [])

    if not files:
        return jsonify({"error": "files list is required"}), 400

    prefix = video_storage_prefix(video_id)
    uploads = []

    for entry in files:
        rel_path = entry.get("path", "").lstrip("/")
        if not rel_path or ".." in rel_path:
            return jsonify({"error": f"invalid path: {rel_path}"}), 400

        object_key = f"{prefix}/{rel_path}"
        content_type = entry.get("content_type") or get_content_type(rel_path)
        url = generate_presigned_put(object_key, content_type=content_type)
        if not url:
            return jsonify({"error": f"failed to presign {rel_path}"}), 500

        uploads.append(
            {
                "path": rel_path,
                "object_key": object_key,
                "upload_url": url,
                "content_type": content_type,
            }
        )

    urls = build_video_urls(video_id)
    return jsonify(
        {
            "video_id": video_id,
            "storage_prefix": prefix,
            "uploads": uploads,
            "manifest_url": urls["hls_manifest_url"],
        }
    ), 200


@app.route("/upload/hls/complete", methods=["POST"])
@jwt_required
def hls_complete():
    data = request.get_json(silent=True) or {}
    video_id = data.get("video_id")
    if not video_id:
        return jsonify({"error": "video_id is required"}), 400

    prefix = video_storage_prefix(video_id)
    for name in ("master.m3u8", "original.mp4"):
        if not object_exists(f"{prefix}/{name}"):
            return jsonify({"error": f"missing required file: {name}"}), 400

    urls = build_video_urls(video_id)
    form_data = {
        "id": video_id,
        "title": data.get("title", "Untitled"),
        "description": data.get("description", ""),
        "uploader": data.get("uploader", "Anonymous"),
        "players": json.dumps(data.get("players", []))
        if isinstance(data.get("players"), list)
        else data.get("players", "[]"),
        "short_id": data.get("short_id") or generate_short_id(),
    }

    video_metadata = {
        "duration": float(data.get("duration", 0)),
        "resolution": data.get("resolution", ""),
        "file_path": "",
        "thumbnail_path": None,
    }

    existing = get_single_document({"_id": video_id})
    if existing:
        update_fields = {
            **urls,
            "encoding_status": "ready",
            "encoding_strategy": "CLIENT",
            "title": form_data["title"],
            "description": form_data["description"],
            "duration": video_metadata["duration"],
            "resolution": video_metadata["resolution"],
        }
        update_db({"_id": video_id}, {"$set": update_fields})
        doc = get_single_document({"_id": video_id})
    else:
        doc = combine_and_save_metadata(
            video_metadata,
            form_data,
            video_id,
            data.get("thumbnail_id"),
            urls["s3_url"],
            extra_fields={
                **urls,
                "encoding_status": "ready",
                "encoding_strategy": "CLIENT",
            },
        )

    return jsonify(
        {"success": True, "metadata": format_video_document(doc)}
    ), 201
