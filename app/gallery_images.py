"""Small, cacheable gallery previews for both generations of replay artwork."""

import io
import time
from contextlib import closing
from functools import lru_cache
from threading import Lock

from bson import ObjectId
from botocore.exceptions import BotoCoreError, ClientError
from flask import Response, abort, request
from gridfs.errors import NoFile
from PIL import Image, ImageOps

from app import app
from app.database import fs
from app.storage import read_location
from app.streaming import resolve_object_key


MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 24_000_000
CACHE_SECONDS = 3600
decode_lock = Lock()


def render_preview(source):
    # Keep concurrent uncached uploads within the API worker's memory budget.
    with decode_lock:
        data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            abort(413)
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > MAX_PIXELS:
                    abort(413)
                image.draft("RGB", (640, 360))
                if image.getexif().get(274, 1) != 1:
                    image = ImageOps.exif_transpose(image)
                width = min(640, image.width, max(1, image.height * 16 // 9))
                height = max(1, round(width * 9 / 16))
                preview = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)
                output = io.BytesIO()
                preview.save(output, "WEBP", quality=74, method=3)
                return output.getvalue()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            abort(415)


@lru_cache(maxsize=128)
def cached_preview(kind, source_id, cache_hour):
    # Only encoded previews are retained. The hour bounds staleness for replaced posters.
    if kind == "thumbnail" and ObjectId.is_valid(source_id):
        try:
            source = fs.get(ObjectId(source_id))
            return render_preview(source)
        except NoFile:
            abort(404)

    key = resolve_object_key(source_id, "poster.jpg") if kind == "poster" else None
    if not key:
        abort(404)
    try:
        client, bucket = read_location(key)
        obj = client.get_object(Bucket=bucket, Key=key)
        with closing(obj["Body"]) as source:
            return render_preview(source)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            abort(404)
        app.logger.exception("Failed to fetch gallery poster")
        abort(503)
    except BotoCoreError:
        app.logger.exception("Gallery poster storage unavailable")
        abort(503)


@app.get("/gallery/<kind>/<source_id>.webp")
def gallery_preview(kind, source_id):
    data = cached_preview(kind, source_id, int(time.monotonic()) // CACHE_SECONDS)
    response = Response(data, mimetype="image/webp", headers={
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Cache-Control": f"public, max-age={CACHE_SECONDS}",
    })
    response.add_etag()
    return response.make_conditional(request)
