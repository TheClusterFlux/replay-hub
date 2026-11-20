"""Public HTML previews for shared replays."""

import re
from urllib.parse import quote, urlencode

from flask import abort, make_response, render_template, request

from app import app
from app.config import API_PUBLIC_BASE_URL, PLAYBACK_UI_ORIGIN
from app.video_metadata import find_video, format_video_document


@app.get("/share")
def share_video():
    video_id = request.args.get("id")
    if not video_id:
        abort(404)
    document = find_video(video_id)
    if document is None:
        abort(404)
    video = format_video_document(document)
    query = urlencode({"id": video["short_id"] or video["id"]})
    ui = PLAYBACK_UI_ORIGIN or "https://replay-hub-ui.theclusterflux.com"
    ready = video["encoding_status"] == "ready"
    poster = f"{API_PUBLIC_BASE_URL}/static/replay-poster.png"
    if video["thumbnail_id"]:
        poster = f"{API_PUBLIC_BASE_URL}/thumbnail/{quote(str(video['thumbnail_id']), safe='')}"
    elif ready and video["poster_url"]:
        poster = video["poster_url"]
    size = re.fullmatch(r"([1-9]\d*)\s*[xX×]\s*([1-9]\d*)", str(video["resolution"]).strip())
    width, height = size.groups() if size else (1280, 720)
    response = make_response(render_template(
        "share.html", title=str(video["title"] or "Replay Hub")[:200],
        description=str(video["description"] or "Watch this replay on Replay Hub.")[:300],
        canonical=f"{ui}/video.html?{query}", watch_url=f"{ui}/video.html?{query}&play=1",
        poster=poster, media=video["inline_video_url"], media_type=video["inline_video_type"],
        width=width, height=height,
    ))
    response.headers["Cache-Control"] = "public, max-age=60" if ready else "no-store"
    return response
