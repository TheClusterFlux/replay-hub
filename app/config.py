import os

# MongoDB Configuration
IS_LOCAL = os.getenv("IS_LOCAL", "false").lower() == "true"

if IS_LOCAL:
    MONGO_URI = "mongodb://localhost:27017"
else:
    MONGO_SERVICE_NAME = os.getenv("MONGO_SERVICE_NAME", "mongodb")
    MONGO_NAMESPACE = os.getenv("MONGO_NAMESPACE", "default")
    MONGO_PORT = int(os.getenv("MONGO_PORT", 27017))
    MONGO_USERNAME = os.getenv("MONGO_USERNAME", "root")
    MONGO_PASSWORD = os.getenv("MONGO_PASSWORD")
    MONGO_URI = f"mongodb://{MONGO_USERNAME}:{MONGO_PASSWORD}@{MONGO_SERVICE_NAME}.{MONGO_NAMESPACE}.svc.cluster.local:{MONGO_PORT}"

UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "./uploads")

# Video encoding: CLIENT (browser WASM) or SERVER (K8s ffmpeg worker)
VIDEO_ENCODING_STRATEGY = os.getenv("VIDEO_ENCODING_STRATEGY", "CLIENT").upper()
if VIDEO_ENCODING_STRATEGY not in ("CLIENT", "SERVER"):
    VIDEO_ENCODING_STRATEGY = "CLIENT"

ENCODING_MAX_CONCURRENT = int(os.getenv("ENCODING_MAX_CONCURRENT", "1"))
ENCODING_FFMPEG_THREADS = int(os.getenv("ENCODING_FFMPEG_THREADS", "2"))
HLS_SEGMENT_SECONDS = int(os.getenv("HLS_SEGMENT_SECONDS", "4"))
PRESIGNED_URL_EXPIRY = int(os.getenv("PRESIGNED_URL_EXPIRY", "3600"))

# Presigned playback (private R2 bucket)
API_PUBLIC_BASE_URL = os.environ.get(
    "API_PUBLIC_BASE_URL", "https://replay-hub.theclusterflux.com"
).rstrip("/")
STREAM_WINDOW_SECONDS = int(os.getenv("STREAM_WINDOW_SECONDS", "300"))
STREAM_LOOKBEHIND_SECONDS = int(os.getenv("STREAM_LOOKBEHIND_SECONDS", "60"))
STREAM_PRESIGN_EXPIRY = int(os.getenv("STREAM_PRESIGN_EXPIRY", "900"))

# Cloudflare R2 (S3-compatible)
USE_R2 = os.getenv("USE_R2", "true").lower() == "true"
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "9d6f2289d9d11dc3d803f10c01f4a23d")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", os.environ.get("R2_ACCESS_KEY", ""))
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", os.environ.get("R2_SECRET_KEY", ""))
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "replay-hub-videos")
R2_ENDPOINT = os.environ.get(
    "R2_ENDPOINT",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
)
# Public URL for browser playback (enable R2 public access or custom domain in Cloudflare)
R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")

# Legacy AWS S3 (migration + fallback)
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY", os.environ.get("AWS_ACCESS_KEY_ID", ""))
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", ""))
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "replay-hub-storage")
S3_REGION = os.environ.get("S3_REGION", "eu-central-1")
