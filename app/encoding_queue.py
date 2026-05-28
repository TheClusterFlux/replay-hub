"""Sequential server-side HLS encoding queue (K8s-friendly)."""

import os
import logging
import threading
import queue
import tempfile
import shutil
from typing import Any, Dict

from app.config import ENCODING_MAX_CONCURRENT
from app.hls_encoder import encode_to_hls
from app.storage import upload_directory, build_video_urls, video_storage_prefix
from app.streaming import build_stream_urls
from app.database import update_db, get_single_document
from app.utils import extract_video_metadata

logger = logging.getLogger(__name__)

_job_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_workers_started = 0
_worker_lock = threading.Lock()


def _process_job(job: Dict[str, Any]) -> None:
    video_id = job["video_id"]
    input_path = job["input_path"]
    form_data = job.get("form_data", {})
    internal_name = job.get("internal_name", video_id)

    logger.info("Starting server encoding for video %s", video_id)

    try:
        update_db(
            {"_id": video_id},
            {
                "$set": {
                    "encoding_status": "processing",
                }
            },
        )

        work_dir = tempfile.mkdtemp(prefix=f"hls_{video_id}_")
        output_dir = os.path.join(work_dir, "hls")

        def progress(phase, step, total):
            logger.info("[%s] %s (%s/%s)", video_id, phase, step, total)

        encode_to_hls(input_path, output_dir, on_progress=progress)

        prefix = video_storage_prefix(video_id)
        if not upload_directory(output_dir, prefix):
            raise RuntimeError("Failed to upload HLS assets to R2")

        urls = build_video_urls(video_id)
        video_metadata = extract_video_metadata(
            os.path.join(output_dir, "original.mp4")
        )

        thumb_id = job.get("thumbnail_id")
        update_fields = {
            **urls,
            "encoding_status": "ready",
            "encoding_strategy": "SERVER",
            "duration": video_metadata.get("duration", 0),
            "resolution": video_metadata.get("resolution", ""),
        }
        if thumb_id:
            update_fields["thumbnail_id"] = thumb_id

        update_db({"_id": video_id}, {"$set": update_fields})
        logger.info("Server encoding complete for %s", video_id)

    except Exception as e:
        logger.exception("Encoding failed for %s: %s", video_id, e)
        update_db(
            {"_id": video_id},
            {"$set": {"encoding_status": "failed", "encoding_error": str(e)}},
        )
    finally:
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except OSError:
                pass
        work = job.get("work_dir")
        if work and os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)


def _worker_loop() -> None:
    while True:
        job = _job_queue.get()
        try:
            _process_job(job)
        finally:
            _job_queue.task_done()


def _ensure_workers() -> None:
    global _workers_started
    with _worker_lock:
        if _workers_started >= ENCODING_MAX_CONCURRENT:
            return
        for _ in range(ENCODING_MAX_CONCURRENT):
            t = threading.Thread(target=_worker_loop, daemon=True)
            t.start()
        _workers_started = ENCODING_MAX_CONCURRENT


def enqueue_server_encoding(
    video_id: str,
    input_path: str,
    form_data: dict,
    internal_name: str,
    thumbnail_id: str = None,
) -> None:
    _ensure_workers()
    _job_queue.put(
        {
            "video_id": video_id,
            "input_path": input_path,
            "form_data": form_data,
            "internal_name": internal_name,
            "thumbnail_id": thumbnail_id,
        }
    )
    logger.info("Queued encoding job for %s (queue size ~%s)", video_id, _job_queue.qsize())


def get_encoding_status(video_id: str) -> Dict[str, Any]:
    doc = get_single_document({"_id": video_id})
    if not doc:
        doc = get_single_document({"short_id": video_id})
    if not doc:
        return {"found": False}
    video_id = doc.get("_id", "")
    stream_urls = build_stream_urls(video_id) if video_id else {}
    return {
        "found": True,
        "video_id": video_id,
        "encoding_status": doc.get("encoding_status", "ready"),
        "encoding_error": doc.get("encoding_error"),
        "hls_manifest_url": stream_urls.get("hls_manifest_url")
        or doc.get("hls_manifest_url")
        or doc.get("s3_url"),
        "download_mp4_url": stream_urls.get("download_mp4_url")
        or doc.get("download_mp4_url"),
    }
