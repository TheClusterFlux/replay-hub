"""Cloudflare R2 / AWS S3-compatible object storage."""

import os
import mimetypes
import logging
import threading
from typing import Optional, List, Dict

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from boto3.s3.transfer import TransferConfig

from app.config import (
    USE_R2,
    R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY,
    R2_BUCKET_NAME,
    R2_ENDPOINT,
    R2_PUBLIC_BASE_URL,
    AWS_ACCESS_KEY,
    AWS_SECRET_KEY,
    S3_BUCKET_NAME,
    S3_REGION,
    PRESIGNED_URL_EXPIRY,
)

logger = logging.getLogger(__name__)

MB = 1024 * 1024
transfer_config = TransferConfig(
    multipart_threshold=64 * MB,
    max_concurrency=10,
    multipart_chunksize=64 * MB,
    use_threads=True,
)

VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m3u": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def get_content_type(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext in VIDEO_TYPES:
        return VIDEO_TYPES[ext]
    content_type, _ = mimetypes.guess_type(file_path)
    return content_type or "application/octet-stream"


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _s3_client():
    return boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


def get_storage_client(use_legacy_s3: bool = False):
    if use_legacy_s3:
        return _s3_client(), S3_BUCKET_NAME
    if USE_R2 and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY:
        return _r2_client(), R2_BUCKET_NAME
    return _s3_client(), S3_BUCKET_NAME


def video_storage_prefix(video_id: str) -> str:
    return f"videos/{video_id}"


def public_url(object_key: str) -> str:
    key = object_key.lstrip("/")
    if R2_PUBLIC_BASE_URL:
        return f"{R2_PUBLIC_BASE_URL}/{key}"
    if USE_R2:
        return f"{R2_ENDPOINT}/{R2_BUCKET_NAME}/{key}"
    return f"https://{S3_BUCKET_NAME}.s3.{S3_REGION}.amazonaws.com/{key}"


def build_video_urls(video_id: str) -> Dict[str, str]:
    from app.streaming import build_stream_urls

    return build_stream_urls(video_id)


def get_object_text(object_key: str, use_legacy_s3: bool = False) -> Optional[str]:
    client, bucket = get_storage_client(use_legacy_s3)
    try:
        response = client.get_object(Bucket=bucket, Key=object_key)
        return response["Body"].read().decode("utf-8")
    except ClientError as e:
        logger.error("Failed to read object %s: %s", object_key, e)
        return None


def upload_file(
    file_path: str,
    object_key: str,
    content_type: Optional[str] = None,
    use_legacy_s3: bool = False,
) -> Optional[str]:
    if not os.path.exists(file_path):
        logger.error("Upload file not found: %s", file_path)
        return None

    client, bucket = get_storage_client(use_legacy_s3)
    if content_type is None:
        content_type = get_content_type(file_path)

    extra_args = {
        "ContentType": content_type,
        "CacheControl": "max-age=31536000",
    }

    try:
        logger.info("Uploading %s -> s3://%s/%s", file_path, bucket, object_key)
        client.upload_file(
            file_path,
            bucket,
            object_key,
            ExtraArgs=extra_args,
            Config=transfer_config,
        )
        url = public_url(object_key)
        logger.info("Upload complete: %s", url)
        return url
    except ClientError as e:
        logger.error("Storage upload failed: %s", e)
        return None


def upload_directory(
    local_dir: str,
    prefix: str,
    use_legacy_s3: bool = False,
) -> bool:
    client, bucket = get_storage_client(use_legacy_s3)
    prefix = prefix.rstrip("/")
    ok = True

    for root, _, files in os.walk(local_dir):
        for name in files:
            local_path = os.path.join(root, name)
            rel = os.path.relpath(local_path, local_dir).replace("\\", "/")
            object_key = f"{prefix}/{rel}"
            content_type = get_content_type(local_path)
            try:
                client.upload_file(
                    local_path,
                    bucket,
                    object_key,
                    ExtraArgs={
                        "ContentType": content_type,
                        "CacheControl": "max-age=31536000",
                    },
                    Config=transfer_config,
                )
                logger.info("Uploaded %s", object_key)
            except ClientError as e:
                logger.error("Failed to upload %s: %s", object_key, e)
                ok = False
    return ok


def generate_presigned_put(
    object_key: str,
    content_type: Optional[str] = None,
    expires_in: int = PRESIGNED_URL_EXPIRY,
) -> Optional[str]:
    client, bucket = get_storage_client()
    params = {"Bucket": bucket, "Key": object_key}
    if content_type:
        params["ContentType"] = content_type
    try:
        return client.generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=expires_in,
        )
    except ClientError as e:
        logger.error("Presign PUT failed for %s: %s", object_key, e)
        return None


def generate_presigned_get(
    object_key: str,
    expires_in: int = PRESIGNED_URL_EXPIRY,
) -> Optional[str]:
    client, bucket = get_storage_client()
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expires_in,
        )
    except ClientError as e:
        logger.error("Presign GET failed for %s: %s", object_key, e)
        return None


def object_exists(object_key: str) -> bool:
    client, bucket = get_storage_client()
    try:
        client.head_object(Bucket=bucket, Key=object_key)
        return True
    except ClientError:
        return False


def list_objects(prefix: str = "", use_legacy_s3: bool = False) -> List[str]:
    client, bucket = get_storage_client(use_legacy_s3)
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def download_object(object_key: str, dest_path: str, use_legacy_s3: bool = False) -> bool:
    client, bucket = get_storage_client(use_legacy_s3)
    try:
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        client.download_file(bucket, object_key, dest_path)
        return True
    except ClientError as e:
        logger.error("Download failed %s: %s", object_key, e)
        return False


def upload_file_async(file_path, object_key=None, content_type=None, callback=None):
    if object_key is None:
        object_key = os.path.basename(file_path)

    def _run():
        result = upload_file(file_path, object_key, content_type)
        if callback:
            callback(result)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


# Backward-compatible aliases used by legacy imports
def upload_to_s3(file_path, object_name=None, content_type=None):
    if object_name is None:
        object_name = os.path.basename(file_path)
    return upload_file(file_path, object_name, content_type)


def upload_to_s3_async(file_path, object_name=None, content_type=None, callback=None):
    upload_file_async(file_path, object_name, content_type, callback)
