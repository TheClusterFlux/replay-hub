#!/usr/bin/env python3
"""
Create the R2 bucket, apply CORS, and print the public URL to add to .env.

Requires R2 API credentials in the environment or .env file.
Run from repo root: python scripts/setup_r2_bucket.py
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def prompt(name: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    value = input(f"{name}{hint}: ").strip()
    return value or default


def main():
    account_id = os.environ.get("R2_ACCOUNT_ID") or prompt(
        "R2_ACCOUNT_ID", "9d6f2289d9d11dc3d803f10c01f4a23d"
    )
    access_key = os.environ.get("R2_ACCESS_KEY_ID") or prompt("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY") or prompt("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET_NAME") or prompt(
        "R2_BUCKET_NAME", "replay-hub-videos"
    )
    endpoint = os.environ.get(
        "R2_ENDPOINT", f"https://{account_id}.r2.cloudflarestorage.com"
    )

    if not access_key or not secret_key:
        print("R2 access key and secret are required.")
        sys.exit(1)

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

    try:
        client.head_bucket(Bucket=bucket)
        print(f"Bucket '{bucket}' already exists.")
    except ClientError:
        print(f"Creating bucket '{bucket}'...")
        try:
            client.create_bucket(Bucket=bucket)
            print("Bucket created.")
        except ClientError as e:
            print(f"Could not create bucket (create it in Cloudflare dashboard if needed): {e}")

    cors = {
        "CORSRules": [
            {
                "AllowedOrigins": [
                    "https://replay-hub.theclusterflux.com",
                    "https://replay-hub-ui.theclusterflux.com",
                    "http://localhost:8080",
                    "http://127.0.0.1:8080",
                ],
                "AllowedMethods": ["GET", "PUT", "HEAD"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
                "MaxAgeSeconds": 3600,
            }
        ]
    }
    try:
        client.put_bucket_cors(Bucket=bucket, CORSConfiguration=cors)
        print("CORS policy applied.")
    except ClientError as e:
        print(f"CORS update failed (set manually in dashboard): {e}")

    print()
    print("=" * 60)
    print("NEXT: Keep this bucket private (disable public access / custom domain).")
    print("Playback uses presigned GET URLs via replay-hub /stream/* routes.")
    print()
    print("Add this block to replay-hub/.env:")
    print()
    print(f"USE_R2=true")
    print(f"VIDEO_ENCODING_STRATEGY=CLIENT")
    print(f"R2_ACCOUNT_ID={account_id}")
    print(f"R2_ACCESS_KEY_ID={access_key}")
    print(f"R2_SECRET_ACCESS_KEY={secret_key}")
    print(f"R2_BUCKET_NAME={bucket}")
    print(f"R2_ENDPOINT={endpoint}")
    print(f"API_PUBLIC_BASE_URL=https://replay-hub.theclusterflux.com")
    print(f"STREAM_WINDOW_SECONDS=300")
    print(f"STREAM_PRESIGN_EXPIRY=900")
    print("=" * 60)


if __name__ == "__main__":
    main()
