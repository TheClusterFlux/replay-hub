"""HLS upload API routes (client presign + complete, config)."""

import json
import time
from pymongo import ReturnDocument
from app.encoding_capacity import CLIENT_LIMIT, SERVER_LIMIT
import math
import re
import posixpath
from pymongo.errors import DuplicateKeyError
import uuid
import logging

from flask import request, jsonify

from app import app
from app.auth import jwt_required
from app.config import VIDEO_ENCODING_STRATEGY, R2_BUCKET_NAME
from app.streaming import storage_is_ready
from app.storage import (
    generate_presigned_put,
    check_write_storage,
    get_content_type,
    get_object_text,
    list_objects,
    video_storage_prefix,
    build_video_urls,
)
from app.database import collection, get_single_document, get_db
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
            "browser_encoding_max_bytes": CLIENT_LIMIT,
            "server_upload_max_bytes": SERVER_LIMIT,
            "r2_bucket": R2_BUCKET_NAME,
            "storage_ready": storage_is_ready(),
        }
    ), 200


@app.route("/api/videos/<video_id>/encoding-status", methods=["GET"])
def encoding_status(video_id):
    return jsonify(get_encoding_status(video_id)), 200


def valid_video_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)


def valid_asset_path(path):
    return (isinstance(path, str) and len(path) <= 512
            and re.fullmatch(r"[\w-]+(?:/[\w-]+)*[.](?:m3u8|ts|mp4|jpg)", path))


def complete_bundle(prefix, expected):
    keys = set(list_objects(prefix + "/"))
    if not all(f"{prefix}/{path}" in keys for path in expected | {"original.mp4", "master.m3u8"}):
        return False
    master = get_object_text(f"{prefix}/master.m3u8") or ""
    variants = [line.strip() for line in master.splitlines() if line.strip() and not line.startswith("#")]
    if not master.startswith("#EXTM3U") or not variants:
        return False
    for path in variants:
        if not valid_asset_path(path) or not path.endswith(".m3u8") or path not in expected:
            return False
        playlist = get_object_text(f"{prefix}/{path}") or ""
        segments = [line.strip() for line in playlist.splitlines() if line.strip() and not line.startswith("#")]
        if not segments or "#EXT-X-ENDLIST" not in playlist:
            return False
        for segment in segments:
            if (not valid_asset_path(segment) or not segment.endswith(".ts")
                    or posixpath.join(posixpath.dirname(path), segment) not in expected):
                return False
    return True


@app.route("/upload/hls/presign", methods=["POST"])
@jwt_required
def hls_presign():
    check_write_storage()
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Expected an upload object"}), 400
    video_id = data.get("video_id") or str(uuid.uuid4())
    files = data.get("files")
    if not valid_video_id(video_id) or not isinstance(files, list) or not files:
        return jsonify({"error": "Invalid video ID or file list"}), 400
    if get_single_document({"_id": video_id}):
        return jsonify({"error": "Video already exists"}), 409

    paths = []
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not valid_asset_path(path):
            return jsonify({"error": "Invalid HLS asset path"}), 400
        paths.append(path)

    # The unique session ID binds all presigns and completion to the first uploader.
    owner = request.current_user_id
    try:
        get_db().hls_uploads.update_one(
            {"_id": video_id, "owner": owner, "state": {"$ne": "deleting"}},
            {"$addToSet": {"paths": {"$each": paths}}, "$set": {"updated": time.time(), "collection": collection.name}}, upsert=True,
        )
    except DuplicateKeyError:
        return jsonify({"error": "Upload belongs to another account"}), 403

    if get_single_document({"_id": video_id}):
        return jsonify({"error": "Video already exists"}), 409

    prefix = video_storage_prefix(video_id)
    uploads = []
    for entry, rel_path in zip(files, paths):
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
    video_id = data.get("video_id") if isinstance(data, dict) else None
    if not valid_video_id(video_id):
        return jsonify({"error": "Invalid video ID"}), 400
    session = get_db().hls_uploads.find_one_and_update(
        {"_id": video_id, "owner": request.current_user_id, "state": {"$ne": "deleting"}},
        {"$set": {"updated": time.time()}}, return_document=ReturnDocument.AFTER)
    if not session:
        return jsonify({"error": "Upload session not found for this account"}), 403
    existing = get_single_document({"_id": video_id})
    if existing:
        if existing.get("uploader_id") != request.current_user_id:
            return jsonify({"error": "Video already exists"}), 409
        return jsonify({"success": True, "metadata": format_video_document(existing)}), 200
    try:
        duration = float(data.get("duration", 0))
        if not math.isfinite(duration) or duration < 0:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        return jsonify({"error": "Invalid duration"}), 400
    prefix = video_storage_prefix(video_id)
    if not complete_bundle(prefix, set(session["paths"])):
        return jsonify({"error": "Incomplete HLS bundle"}), 400

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
        "duration": duration,
        "resolution": data.get("resolution", ""),
        "file_path": "",
        "thumbnail_path": None,
    }

    try:
        doc = combine_and_save_metadata(
            video_metadata,
            form_data,
            video_id,
            None,  # Client HLS bundles provide their own poster.jpg.
            urls["s3_url"],
            extra_fields={
                **urls,
                "encoding_status": "ready",
                "encoding_strategy": "CLIENT",
            },
        )

    except DuplicateKeyError:
        doc = get_single_document({"_id": video_id})
        if not doc or doc.get("uploader_id") != request.current_user_id:
            return jsonify({"error": "Video already exists"}), 409

    get_db().hls_uploads.update_one({"_id": video_id, "state": {"$ne": "deleting"}},
                                     {"$set": {"state": "ready"}})
    return jsonify(
        {"success": True, "metadata": format_video_document(doc)}
    ), 201
