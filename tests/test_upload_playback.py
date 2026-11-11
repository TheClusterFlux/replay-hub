"""Regression checks that run without MongoDB, object storage, or FFmpeg."""

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt

from app import app
from app.video_metadata import format_video_document


class UploadPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.uploads = tempfile.TemporaryDirectory()
        self.addCleanup(self.uploads.cleanup)
        self.enter_patch(patch.dict(app.config, TESTING=True, UPLOAD_FOLDER=self.uploads.name))
        self.enter_patch(patch("app.routes.check_capacity"))
        self.enter_patch(patch("app.routes.check_space"))
        self.enter_patch(patch("app.routes.collection")).find_one.return_value = None
        self.enter_patch(patch("app.routes.reserve", return_value={"token": "f" * 32}))
        self.enter_patch(patch("app.routes.slots")).find_one.return_value = {"token": "f" * 32}
        self.enter_patch(patch("app.auth.User.get_by_id", side_effect=lambda user: SimpleNamespace(
            _id=user, username=user, first_name="", last_name="")))

    def enter_patch(self, mock_patch):
        mock = mock_patch.start()
        self.addCleanup(mock_patch.stop)
        return mock

    def post(self, path, data, user="owner"):
        token = jwt.encode({"user_id": user}, app.config["JWT_SECRET_KEY"], algorithm="HS256")
        return self.client.post(path, data=data, headers={"Authorization": f"Bearer {token}"})

    def test_chunk_retry_does_not_replace_or_double_count_bytes(self):
        form = {"fileId": "upload_1", "fileSize": "2", "totalChunks": "2",
                "filename": "clip.mp4", "action": "init_chunked_upload"}
        self.assertEqual(self.post("/upload/init", form).status_code, 200)

        def chunk(index, content, user="owner"):
            return self.post("/upload/chunk", {"fileId": "upload_1", "totalChunks": "2",
                             "chunkIndex": index, "file": (io.BytesIO(content), "chunk")}, user)

        self.assertEqual(chunk("1", b"b").json["chunksReceived"], 1)
        self.assertEqual(chunk("1", b"b").json["chunksReceived"], 1)
        self.assertEqual(chunk("1", b"x").status_code, 409)
        self.assertEqual(chunk("../escape", b"x").status_code, 400)
        self.assertEqual(chunk("0", b"a", "other").status_code, 400)
        final = {"fileId": "upload_1", "action": "finalize_chunked_upload"}
        self.assertEqual(self.post("/upload/finalize", final).status_code, 400)
        self.assertEqual(chunk("0", b"a").json["chunksReceived"], 2)
        def accept(ticket, path, data):
            self.assertEqual(Path(path).read_bytes(), b"ab")
            return {"success": True, "encoding_status": "pending", "metadata": {"id": data["id"]}}

        enqueue = self.enter_patch(patch("app.routes.accept_source", side_effect=accept))
        accepted = self.post("/upload/finalize", final)
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(self.post("/upload/finalize", final).json, accepted.json)
        enqueue.assert_called_once()

    def test_vod_timeline_and_fresh_segment_redirect(self):
        playlist = "#EXTM3U\n#EXTINF:4,\nseg_00001.ts\n#EXT-X-ENDLIST\n"
        with patch("app.stream_routes.fetch_playlist_text", return_value=playlist):
            response = self.client.get("/stream/replay/720p/index.m3u8?t=315")
            self.assertEqual(response.get_data(as_text=True), playlist)
        with patch("app.stream_routes.generate_presigned_get", return_value="https://storage.test/signed") as sign:
            response = self.client.get("/stream/replay/720p/seg_00001.ts")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(sign.call_args.args[0], "videos/replay/720p/seg_00001.ts")

    def test_migrated_uploader_id_is_json_serializable(self):
        from bson import ObjectId
        owner = ObjectId()
        doc = format_video_document({"_id": "replay", "user_id": owner})
        self.assertEqual(doc["uploader_id"], str(owner))
        app.json.dumps(doc)

    def test_legacy_direct_video_keeps_its_original_url(self):
        url = "https://storage.test/old-clip.mp4"
        doc = format_video_document({"_id": "legacy", "s3_url": url})
        self.assertEqual(doc["s3_url"], url)
        self.assertEqual(doc["download_mp4_url"], url)
        self.assertEqual(self.client.delete("/metadata").status_code, 405)
