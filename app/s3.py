"""Legacy module — delegates to app.storage for R2/S3 operations."""

from app.storage import (  # noqa: F401
    upload_to_s3,
    upload_to_s3_async,
    get_content_type,
    upload_file,
    public_url,
    build_video_urls,
)
