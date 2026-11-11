"""Reclaim owned upload data after its writers and signed PUTs have expired."""
import logging
from pathlib import Path
import re
import shutil
import time

from app.config import PRESIGNED_URL_EXPIRY, UPLOAD_FOLDER
from app.database import collection, get_db
from app.encoding_queue import slots, discard, recover_receiving, UPLOAD_TTL, RECEIVE_TIMEOUT, reserve, release
from app.storage import purge_prefix, video_storage_prefix

log = logging.getLogger(__name__)
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
CLIENT_TTL = max(UPLOAD_TTL, PRESIGNED_URL_EXPIRY + 600)


def mark_deleted(video_id):
    if not SAFE_ID.fullmatch(video_id):
        return
    get_db().hls_uploads.update_one({"_id": video_id}, {"$set": {
        "state": "deleting", "updated": time.time(), "collection": collection.name,
        "purge_after": time.time() + PRESIGNED_URL_EXPIRY + 600}}, upsert=True)


def referenced(vid):
    from bson import ObjectId
    ids = [vid, ObjectId(vid)] if ObjectId.is_valid(vid) else [vid]
    query = {"$or": [{"_id": {"$in": ids}}, {"storage_prefix": video_storage_prefix(vid)}]}
    return any(get_db()[name].find_one(query, {"_id": 1})
               for name in {collection.name, "test_data", "prod_data"})


def clean_session(session, now):
    if session is None:
        return
    vid = session["_id"]
    if not isinstance(vid, str) or not SAFE_ID.fullmatch(vid):
        return
    sessions = get_db().hls_uploads
    # Do not purge an ID referenced by either legacy collection in the same DB.
    if referenced(vid):
        if session.get("state") != "deleting":
            sessions.update_one({"_id": vid, "updated": session["updated"]}, {"$set": {"state": "ready", "updated": now}})
        return
    if session.get("state") != "deleting":
        changed = sessions.update_one({"_id": vid, "updated": session["updated"],
                                       "state": {"$ne": "deleting"}},
                                      {"$set": {"state": "deleting", "purge_after": now}})
        if not changed.matched_count:
            return
    if slots.find_one({"video_id": vid, "state": {"$in": ["pending", "processing", "receiving"]}}):
        return
    if not session.get("purged") or now >= session.get("purge_after", now):
        purge_prefix(video_storage_prefix(vid) + "/")
        if now >= session.get("purge_after", now):
            sessions.delete_one({"_id": vid, "state": "deleting"})
        else:
            sessions.update_one({"_id": vid}, {"$set": {"purged": True}})


def clean_local(now):
    root = Path(UPLOAD_FOLDER)
    for path in root.iterdir():
        if path.is_symlink():
            continue
        try:
            if now - path.stat().st_mtime < UPLOAD_TTL:
                continue
            if path.is_dir() and path.name.startswith(("chunks_", "receive-", "encode-")):
                claims = [{"local_dir": path.name}]
                if path.name.startswith("encode-"):
                    claims.append({"token": path.name.split("-", 2)[1]})
                if slots.find_one({"$or": claims, "state": {"$ne": "discard"}}):
                    continue
                shutil.rmtree(path)
            elif path.is_file() and re.fullmatch(r"[\da-f-]{36}\.[\w]+(?:_thumbnail\.jpg)?", path.name):
                if not collection.find_one({"$or": [{"internal_name": path.stem}, {"_id": path.stem}], "encoding_status":
                                            {"$in": ["pending", "processing"]}}, {"_id": 1}):
                    path.unlink()
        except FileNotFoundError:
            continue


def find_untracked(bucket_name):
    """Inventory an explicitly selected write bucket. Ignore recent and referenced data."""
    from app.storage import check_write_storage, get_storage_client
    check_write_storage()
    client, active_bucket = get_storage_client()
    if bucket_name != active_bucket:
        raise ValueError("Bucket must match the configured write bucket")
    cutoff = time.time() - CLIENT_TTL
    def unclaimed(vid):
        return (SAFE_ID.fullmatch(vid) and not referenced(vid)
                and not slots.find_one({"video_id": vid}) and not get_db().hls_uploads.find_one({"_id": vid}))

    # S3 omits unfinished multipart uploads from its object listing.
    multipart = {}
    uploads = client.get_paginator("list_multipart_uploads")
    for page in uploads.paginate(Bucket=active_bucket, Prefix="videos/"):
        for upload in page.get("Uploads", []):
            parts = upload["Key"].split("/", 2)
            if len(parts) == 3:
                vid = parts[1]
                multipart[vid] = max(multipart.get(vid, 0), upload["Initiated"].timestamp())

    pages = client.get_paginator("list_objects_v2")
    for page in pages.paginate(Bucket=active_bucket, Prefix="videos/", Delimiter="/"):
        for entry in page.get("CommonPrefixes", []):
            prefix = entry["Prefix"]
            vid = prefix.split("/")[1]
            if multipart.pop(vid, 0) >= cutoff or not unclaimed(vid):
                continue
            objects = (obj for group in pages.paginate(Bucket=active_bucket, Prefix=prefix)
                       for obj in group.get("Contents", []))
            if all(obj["LastModified"].timestamp() < cutoff for obj in objects):
                yield vid
    for vid, last_write in multipart.items():
        if last_write < cutoff and unclaimed(vid):
            yield vid


def sweep():
    now = time.time()
    recover_receiving()
    slots.update_many({"state": "receiving", "updated": {"$lt": now - RECEIVE_TIMEOUT}},
                      {"$set": {"state": "discard", "expired": True}})
    for ticket in slots.find({"state": "discard"}):
        try:
            discard(ticket)
        except Exception:
            log.exception("Will retry staging cleanup")
    sessions = get_db().hls_uploads
    # Older presigns have no timestamp. Give them a full grace period on upgrade.
    sessions.update_many({"updated": {"$exists": False}}, {"$set": {"updated": now}})
    for session in sessions.find({"$or": [{"state": "deleting"},
                                          {"updated": {"$lt": now - CLIENT_TTL}}],
                                  "collection": {"$in": [None, collection.name]}}):
        try:
            clean_session(session, now)
        except Exception:
            log.exception("Will retry upload cleanup for %s", session["_id"])
    clean_local(now)

def adopt_legacy_job():
    """Recover pre-upgrade pending records when their original local file still exists."""
    from app.encoding_capacity import check_capacity, probe_source, scratch_bytes
    from app.storage import build_video_urls, upload_file
    from werkzeug.exceptions import HTTPException, ServiceUnavailable

    query = {"encoding_strategy": "SERVER", "encoding_status": {"$in": ["processing", "pending"]},
             "encoding_ticket": {"$exists": False}}
    for doc in collection.find(query).limit(10):
        legacy_id = doc["_id"]
        match = {**query, "_id": legacy_id}
        if not isinstance(legacy_id, str) or not SAFE_ID.fullmatch(legacy_id):
            collection.update_one(match, {"$set": {"encoding_status": "failed", "encoding_error": "Please re-upload this pre-upgrade replay."}})
            continue
        path = None
        for stem in (doc.get("internal_name"), legacy_id):
            if isinstance(stem, str) and SAFE_ID.fullmatch(stem):
                path = next((f for f in Path(UPLOAD_FOLDER).glob(stem + ".*") if f.is_file()
                             and not f.is_symlink() and f.suffix.lower() in
                             {".mp4", ".webm", ".mkv", ".avi", ".flv", ".mov", ".wmv", ".m4v"}), None)
                if path:
                    break
        if not path:
            collection.update_one(match, {"$set": {"encoding_status": "failed",
                                  "encoding_error": "The original pre-upgrade upload is missing. Please upload it again."}})
            continue
        size = path.stat().st_size
        try:
            ticket = reserve("legacy-" + legacy_id, "migration", size)
        except ServiceUnavailable:
            return
        try:
            check_capacity(size, UPLOAD_FOLDER)
            info = probe_source(path)
            key = "upload-staging/" + ticket["token"] + "/source"
            claimed = slots.update_one({"token": ticket["token"], "state": "receiving", "key": {"$exists": False}},
                                       {"$set": {"key": key, "video_id": legacy_id, "suffix": path.suffix,
                                                 "scratch": scratch_bytes(size, info["duration"])}})
            if not claimed.matched_count:
                return
            if not upload_file(str(path), key):
                raise OSError("Source staging failed")
            recovered = {**build_video_urls(legacy_id), "encoding_status": "pending",
                         "encoding_ticket": ticket["token"], "duration": info["duration"],
                         "resolution": info["resolution"]}
            if collection.update_one(match, {"$set": recovered}).matched_count:
                slots.update_one({"token": ticket["token"]}, {"$set": {"state": "pending"}})
                path.unlink(missing_ok=True)
            else:
                release(ticket)
        except HTTPException as exc:
            release(ticket)
            if exc.code != 503:
                collection.update_one(match, {"$set": {"encoding_status": "failed", "encoding_error": exc.description}})
        except Exception:
            if not collection.find_one({"_id": legacy_id, "encoding_ticket": ticket["token"]}):
                release(ticket)
            raise
        return

if __name__ == "__main__":
    import argparse
    from pymongo.errors import DuplicateKeyError
    parser = argparse.ArgumentParser(description="Find old untracked Replay Hub video prefixes in a dedicated bucket")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for video_id in find_untracked(args.bucket):
        print(video_storage_prefix(video_id) + "/")
        if args.apply:
            # Take the same unique ID used by the upload routes before the final reference check.
            session = {"_id": video_id, "state": "deleting", "updated": time.time(),
                       "purge_after": time.time() + CLIENT_TTL, "collection": collection.name}
            try:
                get_db().hls_uploads.insert_one(dict(session))
            except DuplicateKeyError:
                continue
            clean_session(session, time.time())
