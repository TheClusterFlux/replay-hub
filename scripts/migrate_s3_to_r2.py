#!/usr/bin/env python3
"""
One-time migration: AWS S3 MP4 videos → R2 HLS bundles.

Usage:
  python scripts/migrate_s3_to_r2.py --dry-run
  python scripts/migrate_s3_to_r2.py --limit 5
  python scripts/migrate_s3_to_r2.py --video-id <mongo_id>
"""

import os
import sys
import json
import argparse
import tempfile
import shutil
from urllib.parse import urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass


def prompt(name: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    value = input(f"{name}{hint}: ").strip()
    return value or default


def ensure_r2_env():
    """Load or interactively collect R2 credentials."""
    keys = {
        "R2_ACCOUNT_ID": os.environ.get("R2_ACCOUNT_ID", ""),
        "R2_ACCESS_KEY_ID": os.environ.get("R2_ACCESS_KEY_ID", ""),
        "R2_SECRET_ACCESS_KEY": os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        "R2_BUCKET_NAME": os.environ.get("R2_BUCKET_NAME", ""),
        "R2_ENDPOINT": os.environ.get("R2_ENDPOINT", ""),
        "R2_PUBLIC_BASE_URL": os.environ.get("R2_PUBLIC_BASE_URL", ""),
    }

    missing = [k for k, v in keys.items() if not v]
    if missing:
        print("\nR2 configuration incomplete. Enter values (paste from Cloudflare dashboard):\n")
        keys["R2_ACCOUNT_ID"] = keys["R2_ACCOUNT_ID"] or prompt(
            "R2_ACCOUNT_ID", "9d6f2289d9d11dc3d803f10c01f4a23d"
        )
        keys["R2_ACCESS_KEY_ID"] = keys["R2_ACCESS_KEY_ID"] or prompt("R2_ACCESS_KEY_ID")
        keys["R2_SECRET_ACCESS_KEY"] = keys["R2_SECRET_ACCESS_KEY"] or prompt(
            "R2_SECRET_ACCESS_KEY"
        )
        keys["R2_BUCKET_NAME"] = keys["R2_BUCKET_NAME"] or prompt(
            "R2_BUCKET_NAME", "replay-hub-videos"
        )
        keys["R2_ENDPOINT"] = keys["R2_ENDPOINT"] or prompt(
            "R2_ENDPOINT",
            f"https://{keys['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        )
        keys["R2_PUBLIC_BASE_URL"] = keys["R2_PUBLIC_BASE_URL"] or prompt(
            "R2_PUBLIC_BASE_URL (public r2.dev or custom domain URL)"
        )

        print("\n--- Copy into replay-hub/.env ---\n")
        for k, v in keys.items():
            print(f"{k}={v}")
        print("USE_R2=true")
        print("VIDEO_ENCODING_STRATEGY=CLIENT")
        print("---\n")

        for k, v in keys.items():
            os.environ[k] = v
        os.environ["USE_R2"] = "true"

    return keys


def s3_key_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return parsed.path.lstrip("/")


def main():
    parser = argparse.ArgumentParser(description="Migrate S3 MP4s to R2 HLS")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--video-id", type=str, default="")
    parser.add_argument("--checkpoint", default=os.path.join(ROOT, "migrate_checkpoint.json"))
    args = parser.parse_args()

    ensure_r2_env()

    # Reload app modules after env is set
    import importlib
    import app.config as config
    importlib.reload(config)

    from pymongo import MongoClient
    from app.config import MONGO_URI, IS_LOCAL
    from app.hls_encoder import encode_to_hls
    from app.storage import (
        download_object,
        upload_directory,
        build_video_urls,
        list_objects,
        video_storage_prefix,
    )

    collection_name = "test_data" if IS_LOCAL else "prod_data"
    client = MongoClient(os.environ.get("MONGO_URI", MONGO_URI))
    db = client["replay_hub"]
    collection = db[collection_name]

    checkpoint = {}
    if os.path.exists(args.checkpoint):
        with open(args.checkpoint, "r", encoding="utf-8") as f:
            checkpoint = json.load(f)

    query = {}
    if args.video_id:
        query["_id"] = args.video_id

    videos = list(collection.find(query))
    print(f"Found {len(videos)} video documents in {collection_name}")

    migrated = 0
    for doc in videos:
        vid = doc.get("_id")
        if checkpoint.get(vid) == "done":
            print(f"[skip] {vid} already migrated")
            continue

        s3_url = doc.get("s3_url", "")
        if s3_url.endswith(".m3u8") or doc.get("encoding_status") == "ready" and doc.get("hls_manifest_url"):
            print(f"[skip] {vid} already HLS")
            continue

        if not s3_url or s3_url.startswith("/uploads"):
            print(f"[skip] {vid} no remote S3 URL")
            continue

        s3_key = s3_key_from_url(s3_url)
        if not s3_key:
            print(f"[warn] {vid} could not parse S3 key from {s3_url}")
            continue

        print(f"\n=== Migrating {vid} ({doc.get('title', '')}) ===")
        print(f"  S3 key: {s3_key}")

        if args.dry_run:
            print("  [dry-run] would download, encode, upload to R2")
            migrated += 1
            if args.limit and migrated >= args.limit:
                break
            continue

        work = tempfile.mkdtemp(prefix=f"migrate_{vid}_")
        try:
            local_mp4 = os.path.join(work, "source.mp4")
            print("  Downloading from S3...")
            if not download_object(s3_key, local_mp4, use_legacy_s3=True):
                print(f"  ERROR: download failed for {s3_key}")
                continue

            output_dir = os.path.join(work, "hls")
            print("  Encoding HLS ladder...")

            def progress(phase, step, total):
                print(f"    [{step}/{total}] {phase}")

            encode_to_hls(local_mp4, output_dir, on_progress=progress)

            prefix = video_storage_prefix(vid)
            print(f"  Uploading to R2 prefix {prefix}...")
            if not upload_directory(output_dir, prefix):
                print("  ERROR: R2 upload failed")
                continue

            urls = build_video_urls(vid)
            collection.update_one(
                {"_id": vid},
                {
                    "$set": {
                        **urls,
                        "encoding_status": "ready",
                        "encoding_strategy": "MIGRATED",
                    }
                },
            )
            checkpoint[vid] = "done"
            with open(args.checkpoint, "w", encoding="utf-8") as f:
                json.dump(checkpoint, f, indent=2)

            print(f"  Done → {urls['hls_manifest_url']}")
            migrated += 1
        finally:
            shutil.rmtree(work, ignore_errors=True)

        if args.limit and migrated >= args.limit:
            break

    print(f"\nMigration finished. Processed {migrated} video(s).")


if __name__ == "__main__":
    main()
