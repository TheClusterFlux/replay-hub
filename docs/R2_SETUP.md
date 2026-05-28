# Cloudflare R2 setup for Replay Hub

## Where playback URLs come from

Videos are stored in R2 under:

```
videos/{video_id}/master.m3u8
videos/{video_id}/original.mp4
videos/{video_id}/480p/...
```

**Playback URLs are not the private S3 API endpoint.** Browsers need a **public** base URL:

1. Create bucket `replay-hub-videos` (or your chosen name).
2. In Cloudflare: **R2 → bucket → Settings → Public access → Allow**.
3. Copy the **`*.r2.dev`** URL (or attach a custom domain).
4. Set `R2_PUBLIC_BASE_URL` in `.env` to that URL (no trailing slash).

Example: if public URL is `https://pub-abc123.r2.dev`, a manifest is:

`https://pub-abc123.r2.dev/videos/{video_id}/master.m3u8`

## Quick setup

```bash
cd replay-hub
pip install python-dotenv boto3
python scripts/setup_r2_bucket.py
```

Paste the printed block into `.env`, set `R2_PUBLIC_BASE_URL` after enabling public access, restart the API.

## One-time S3 migration

```bash
python scripts/migrate_s3_to_r2.py --dry-run
python scripts/migrate_s3_to_r2.py --limit 1
python scripts/migrate_s3_to_r2.py
```

Requires AWS keys and MongoDB access in `.env`.

## Encoding strategy

| Value | Behavior |
|-------|----------|
| `CLIENT` (default) | Browser encodes HLS via ffmpeg.wasm, uploads to R2 with presigned URLs |
| `SERVER` | Raw MP4 uploaded to API, K8s worker encodes sequentially with ffmpeg |

Toggle: `VIDEO_ENCODING_STRATEGY=CLIENT|SERVER`
