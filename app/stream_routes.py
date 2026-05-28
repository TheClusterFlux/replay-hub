"""HTTP routes for presigned HLS streaming."""

import logging

from flask import Response, redirect, request, jsonify

from app import app
from app.storage import generate_presigned_get
from app.config import STREAM_PRESIGN_EXPIRY
from app.streaming import (
    fetch_playlist_text,
    resolve_object_key,
    rewrite_master_playlist,
    rewrite_variant_playlist,
    segment_prefix_for_playlist,
)

logger = logging.getLogger(__name__)


def _playback_time() -> float:
    raw = request.args.get("t", "0")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


@app.route("/stream/<video_id>/download", methods=["GET"])
def stream_download(video_id):
    object_key = resolve_object_key(video_id, "original.mp4")
    if not object_key:
        return jsonify({"error": "invalid path"}), 400
    signed = generate_presigned_get(object_key, expires_in=STREAM_PRESIGN_EXPIRY)
    if not signed:
        return jsonify({"error": "object not found"}), 404
    return redirect(signed, 302)


@app.route("/stream/<video_id>/poster.jpg", methods=["GET"])
def stream_poster(video_id):
    object_key = resolve_object_key(video_id, "poster.jpg")
    if not object_key:
        return jsonify({"error": "invalid path"}), 400
    signed = generate_presigned_get(object_key, expires_in=STREAM_PRESIGN_EXPIRY)
    if not signed:
        return jsonify({"error": "object not found"}), 404
    return redirect(signed, 302)


@app.route("/stream/<video_id>/master.m3u8", methods=["GET"])
def stream_master(video_id):
    object_key = resolve_object_key(video_id, "master.m3u8")
    if not object_key:
        return jsonify({"error": "invalid path"}), 400

    raw = fetch_playlist_text(object_key)
    if raw is None:
        return jsonify({"error": "manifest not found"}), 404

    body = rewrite_master_playlist(raw, video_id)
    return Response(
        body,
        mimetype="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/stream/<video_id>/<path:rel_path>", methods=["GET"])
def stream_playlist(video_id, rel_path):
    if not rel_path.endswith(".m3u8"):
        return jsonify({"error": "not found"}), 404

    object_key = resolve_object_key(video_id, rel_path)
    if not object_key:
        return jsonify({"error": "invalid path"}), 400

    raw = fetch_playlist_text(object_key)
    if raw is None:
        return jsonify({"error": "playlist not found"}), 404

    try:
        body = rewrite_variant_playlist(
            raw,
            segment_prefix_for_playlist(video_id, rel_path),
            _playback_time(),
        )
    except RuntimeError as exc:
        logger.error("Playlist rewrite failed for %s: %s", object_key, exc)
        return jsonify({"error": str(exc)}), 500

    return Response(
        body,
        mimetype="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )
