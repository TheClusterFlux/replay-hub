"""HTTP routes for presigned HLS streaming."""

import logging

from flask import Response, redirect, jsonify, request

from app import app
from app.storage import generate_presigned_get, read_location
from botocore.exceptions import BotoCoreError, ClientError
from app.config import STREAM_PRESIGN_EXPIRY, PLAYBACK_UI_ORIGIN, API_PUBLIC_BASE_URL
from app.streaming import (
    fetch_playlist_text,
    resolve_object_key,
    rewrite_master_playlist,
)

logger = logging.getLogger(__name__)


@app.route("/stream/<video_id>/download", methods=["GET"])
def stream_download(video_id):
    object_key = resolve_object_key(video_id, "original.mp4")
    if not object_key:
        return jsonify({"error": "invalid path"}), 400
    signed = generate_presigned_get(object_key, expires_in=STREAM_PRESIGN_EXPIRY, attachment=True)
    if not signed:
        return jsonify({"error": "object not found"}), 404
    return redirect(signed, 302)


@app.route("/stream/<video_id>/poster.jpg", methods=["GET"])
def stream_poster(video_id):
    object_key = resolve_object_key(video_id, "poster.jpg")
    if not object_key:
        return jsonify({"error": "invalid path"}), 400
    try:
        client, bucket = read_location(object_key)
        obj = client.get_object(Bucket=bucket, Key=object_key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return jsonify({"error": "poster not found"}), 404
        logger.exception("Failed to read poster for %s", video_id)
        return jsonify({"error": "poster unavailable"}), 503
    except BotoCoreError:
        logger.exception("Poster storage unavailable")
        return jsonify({"error": "poster unavailable"}), 503

    # Serve the image from the API origin. A second cross-origin redirect
    # changes the browser's Origin to null, which the private bucket rejects.
    body = obj["Body"]
    response = Response(body.iter_chunks(64 * 1024), mimetype="image/jpeg", headers={
        "Content-Length": str(obj["ContentLength"]),
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Cache-Control": "public, max-age=3600",
    })
    response.call_on_close(body.close)
    return response


@app.route("/stream/<video_id>/master.m3u8", methods=["GET"])
def stream_master(video_id):
    object_key = resolve_object_key(video_id, "master.m3u8")
    if not object_key:
        return jsonify({"error": "invalid path"}), 400

    raw = fetch_playlist_text(object_key)
    if raw is None:
        return jsonify({"error": "manifest not found"}), 404

    # The UI gateway serves /stream on its own origin. Relative segments there
    # can redirect to private storage without losing the browser's Origin.
    base = PLAYBACK_UI_ORIGIN if PLAYBACK_UI_ORIGIN and request.headers.get("Origin") == PLAYBACK_UI_ORIGIN else API_PUBLIC_BASE_URL
    body = rewrite_master_playlist(raw, video_id, base)
    return Response(
        body,
        mimetype="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store", "Vary": "Origin"},
    )


@app.route("/stream/<video_id>/<path:rel_path>", methods=["GET"])
def stream_playlist(video_id, rel_path):
    if not rel_path.endswith((".m3u8", ".ts")):
        return jsonify({"error": "not found"}), 404

    object_key = resolve_object_key(video_id, rel_path)
    if not object_key:
        return jsonify({"error": "invalid path"}), 400

    if rel_path.endswith(".ts"):
        signed = generate_presigned_get(object_key, expires_in=STREAM_PRESIGN_EXPIRY)
        if not signed:
            return jsonify({"error": "segment unavailable"}), 503
        response = redirect(signed, 302)
        response.headers["Cache-Control"] = "no-store"
        return response

    raw = fetch_playlist_text(object_key)
    if raw is None:
        return jsonify({"error": "playlist not found"}), 404

    return Response(
        raw,
        mimetype="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )
