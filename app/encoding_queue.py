"""Bounded MongoDB queue. Accepted inputs live in private object storage."""
import json
import logging
import shutil
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from werkzeug.exceptions import Conflict, ServiceUnavailable

from app.config import UPLOAD_FOLDER
from app.database import collection, get_single_document
from app.encoding_capacity import QUEUE_LIMIT, check_memory, check_space, probe_source, scratch_bytes
from app.hls_encoder import encode_to_hls
from app.storage import (build_video_urls, download_object, purge_prefix,
                         upload_directory, upload_file, video_storage_prefix)
from app.video_metadata import combine_and_save_metadata, format_video_document

log = logging.getLogger(__name__)
slots = collection.database[collection.name + "_encoding_slots"]
locks = collection.database[collection.name + "_worker_locks"]
LEASE = 90
UPLOAD_TTL = 86400
RECEIVE_TIMEOUT = 1800
_start_lock = threading.Lock()
_worker_pid = None
_wake = threading.Event()


def reserve(session, owner, size):
    now = time.time()
    for number in range(QUEUE_LIMIT):
        ticket = {"_id": number, "token": uuid.uuid4().hex, "session": session,
                  "owner": owner, "size": size, "state": "receiving", "updated": now}
        try:
            slots.insert_one(ticket)
            return ticket
        except DuplicateKeyError:
            existing = slots.find_one({"session": session, "owner": owner})
            if existing:
                if existing["size"] != size or existing["state"] != "receiving":
                    raise Conflict("This upload ID is already in use.")
                return existing
    raise ServiceUnavailable("The encoding queue is full. Wait for a replay to finish, then retry.")


def check_queue():
    if slots.count_documents({}) >= QUEUE_LIMIT:
        raise ServiceUnavailable("The encoding queue is full. Wait for a replay to finish, then retry.")


def release(ticket):
    # Keep a durable cleanup record when object storage is unavailable.
    query = {"token": ticket["token"], "state": "receiving"}
    current = slots.find_one_and_update(query, {"$set": {"state": "discard"}},
                                        return_document=ReturnDocument.AFTER)
    if current:
        try:
            discard(current)
        except Exception:
            log.exception("Staging cleanup will be retried")


def discard(ticket):
    name = ticket.get("local_dir")
    if ticket.get("expired") and name and Path(name).name == name and name.startswith("chunks_"):
        directory = Path(UPLOAD_FOLDER) / name
        metadata = directory / "metadata.json"
        if not directory.is_symlink() and metadata.is_file():
            if json.loads(metadata.read_text()).get("ticket") == ticket["token"]:
                shutil.rmtree(directory)
    if ticket.get("key"):
        purge_prefix("upload-staging/" + ticket["token"] + "/")
    failed = {"_id": ticket.get("video_id"), "encoding_ticket": ticket["token"], "encoding_status": "failed"}
    if ticket.get("video_id") and collection.find_one(failed, {"_id": 1}):
        purge_prefix(video_storage_prefix(ticket["video_id"]) + "/")
    slots.delete_one({"token": ticket["token"], "state": "discard"})


def accept_source(ticket, path, form_data):
    info = probe_source(path)
    size = os.path.getsize(path)
    needed = scratch_bytes(size, info["duration"])
    check_space(os.path.dirname(path), needed)
    token = ticket["token"]
    key = f"upload-staging/{token}/source"
    video_id = form_data["id"]
    fields = {"video_id": video_id, "key": key, "suffix": Path(path).suffix,
              "scratch": needed, "updated": time.time()}
    if not slots.update_one({"token": token, "state": "receiving"}, {"$set": fields}).matched_count:
        raise Conflict("The upload expired. Start it again.")
    if not upload_file(str(path), key, "application/octet-stream"):
        raise ServiceUnavailable("Object storage could not save the source. Please retry.")
    urls = build_video_urls(video_id)
    # The source and this insert are durable before the API can acknowledge a job.
    # A restart between the insert and the state update is reconciled by the worker.
    doc = combine_and_save_metadata(info, form_data, Path(path).stem, None, urls["s3_url"],
                                    extra_fields={**urls, "encoding_status": "pending",
                                                  "encoding_strategy": "SERVER", "encoding_ticket": token})
    slots.update_one({"token": token, "state": "receiving"},
                     {"$set": {"state": "pending", "updated": time.time()}})
    _wake.set()
    return {"success": True, "encoding_status": "pending", "metadata": format_video_document(doc)}


def acquire_worker(owner):
    now = time.time()
    try:
        return locks.find_one_and_update(
            {"_id": "encoder", "$or": [{"owner": owner}, {"until": {"$lt": now}}]},
            {"$set": {"owner": owner, "until": now + LEASE}}, upsert=True,
            return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        return None


def recover_receiving():
    for ticket in slots.find({"state": "receiving", "video_id": {"$exists": True}}):
        if collection.find_one({"_id": ticket["video_id"], "encoding_ticket": ticket["token"]}, {"_id": 1}):
            slots.update_one({"token": ticket["token"], "state": "receiving"}, {"$set": {"state": "pending"}})


def process_next(owner):
    if not acquire_worker(owner):
        return False
    recover_receiving()
    ticket = slots.find_one_and_update(
        {"state": {"$in": ["pending", "processing"]}},
        {"$set": {"state": "processing", "worker": owner}, "$inc": {"attempts": 1}},
        sort=[("updated", 1)], return_document=ReturnDocument.AFTER)
    if not ticket:
        return False
    token, video_id = ticket["token"], ticket["video_id"]
    claim = {"token": token, "state": "processing", "worker": owner}
    video = {"_id": video_id, "encoding_ticket": token}
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not stop.wait(LEASE / 3):
            try:
                if locks.update_one({"_id": "encoder", "owner": owner},
                                    {"$set": {"until": time.time() + LEASE}}).matched_count:
                    continue
            except Exception:
                log.exception("Worker lease renewal failed")
            lost.set()
            return

    keeper = threading.Thread(target=heartbeat, daemon=True)
    keeper.start()
    try:
        doc = collection.find_one(video, {"encoding_status": 1})
        if doc and doc["encoding_status"] in ("pending", "processing"):
            if ticket["attempts"] > 3:
                raise ServiceUnavailable("The server repeatedly stopped while encoding. Trim or downscale this clip.")
            collection.update_one(video, {"$set": {"encoding_status": "processing"}})
            check_memory()
            check_space(UPLOAD_FOLDER, ticket["scratch"])
            with tempfile.TemporaryDirectory(prefix="encode-" + token + "-", dir=UPLOAD_FOLDER) as work:
                source = os.path.join(work, "input" + ticket["suffix"])
                output = os.path.join(work, "hls")
                if not download_object(ticket["key"], source):
                    raise ServiceUnavailable("The stored source could not be read. Please retry the upload.")
                encode_to_hls(source, output)
                if lost.is_set() or not slots.find_one(claim) or not collection.find_one(video, {"_id": 1}):
                    return True
                if not upload_directory(output, video_storage_prefix(video_id)):
                    raise ServiceUnavailable("The server could not store the encoded video. Please retry.")
                if not lost.is_set() and slots.find_one(claim):
                    collection.update_one(video, {"$set": {"encoding_status": "ready"}})
    except Exception as exc:
        log.exception("Encoding failed for %s", video_id)
        if lost.is_set() or not slots.find_one(claim):
            return True
        message = exc.description if isinstance(exc, ServiceUnavailable) else (
            "The server could not encode this video within its resources. Try a shorter or lower-resolution file.")
        collection.update_one(video, {"$set": {"encoding_status": "failed", "encoding_error": message}})
    finally:
        stop.set()
        keeper.join()
    if not lost.is_set() and slots.update_one(claim, {"$set": {"state": "discard"}}).matched_count:
        discard(ticket)
    return True


def _run():
    from app.upload_cleanup import sweep, adopt_legacy_job
    owner = uuid.uuid4().hex
    last_sweep = 0
    indexed = False
    while True:
        try:
            if not indexed:
                slots.create_index([("owner", 1), ("session", 1)], unique=True)
                indexed = True
            if acquire_worker(owner):
                if time.monotonic() - last_sweep > 60 or not last_sweep:
                    last_sweep = time.monotonic()
                    try:
                        sweep()
                        adopt_legacy_job()
                    except Exception:
                        log.exception("Upload maintenance will retry")
                if process_next(owner):
                    continue
        except Exception:
            log.exception("Upload worker will retry")
        _wake.wait(3)
        _wake.clear()


def start_workers():
    global _worker_pid
    with _start_lock:
        if _worker_pid != os.getpid():
            threading.Thread(target=_run, name="replay-encoder", daemon=True).start()
            _worker_pid = os.getpid()


def get_encoding_status(video_id):
    doc = get_single_document({"_id": video_id}) or get_single_document({"short_id": video_id})
    if not doc:
        return {"found": False}
    result = format_video_document(doc)
    return {"found": True, "video_id": result["id"], "encoding_status": result["encoding_status"],
            "encoding_error": doc.get("encoding_error"), "hls_manifest_url": result["hls_manifest_url"],
            "download_mp4_url": result["download_mp4_url"]}
