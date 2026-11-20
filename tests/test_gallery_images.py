"""Poster responses must be readable from the cross-origin isolated gallery."""
import io
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from PIL import Image
from app import app
from app.gallery_images import cached_preview
from app.video_metadata import format_video_document


class GalleryImageTests(unittest.TestCase):
    def setUp(self):
        settings = patch.dict(app.config, TESTING=True)
        settings.start()
        self.addCleanup(settings.stop)
        self.client = app.test_client()
        self.addCleanup(cached_preview.cache_clear)

    @staticmethod
    def jpeg(size):
        stream = io.BytesIO()
        Image.new("RGB", size, "teal").save(stream, "JPEG")
        return stream.getvalue()

    def test_optimized_thumbnail_is_cached_and_original_is_preserved(self):
        thumbnail = "000000000000000000000001"
        url = "/gallery/thumbnail/" + thumbnail + ".webp"
        original = self.jpeg((1920, 1080))
        with patch("app.gallery_images.fs") as fs:
            fs.get.return_value = io.BytesIO(original)
            reply = self.client.get(url)
            self.assertEqual(reply.mimetype, "image/webp")
            self.assertEqual(reply.headers["Cross-Origin-Resource-Policy"], "cross-origin")
            with Image.open(io.BytesIO(reply.data)) as preview:
                self.assertEqual(preview.size, (640, 360))
            self.assertLess(len(reply.data), len(original))
            self.assertEqual(self.client.get(url, headers={"If-None-Match": reply.headers["ETag"]}).status_code, 304)
            fs.get.assert_called_once()
        with patch("app.routes.fs") as fs:
            fs.get.return_value = io.BytesIO(original)
            self.assertEqual(self.client.get("/thumbnail/" + thumbnail).data, original)

    def test_poster_only_preview_works_for_existing_storage_and_small_images(self):
        data = self.jpeg((160, 240))
        stream = io.BytesIO(data)
        store = MagicMock()
        store.get_object.return_value = {"Body": StreamingBody(stream, len(data))}
        with patch("app.gallery_images.read_location", return_value=(store, "historical")):
            reply = self.client.get("/gallery/poster/old-replay.webp")
        store.get_object.assert_called_once_with(Bucket="historical", Key="videos/old-replay/poster.jpg")
        self.assertTrue(stream.closed)
        with Image.open(io.BytesIO(reply.data)) as preview:
            self.assertEqual(preview.size, (160, 90))

    def test_failed_previews_can_be_retried(self):
        with patch("app.gallery_images.fs") as fs:
            fs.get.side_effect = [io.BytesIO(b"bad image"), io.BytesIO(self.jpeg((640, 360)))]
            url = "/gallery/thumbnail/000000000000000000000002.webp"
            self.assertEqual(self.client.get(url).status_code, 415)
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_metadata_advertises_only_locally_managed_preview_sources(self):
        video = format_video_document({"_id": "old", "storage_prefix": "videos/old", "thumbnail_id": "abc"})
        self.assertEqual([u.split("/gallery/")[1] for u in video["gallery_image_urls"]],
                         ["thumbnail/abc.webp", "poster/old.webp"])
        legacy = format_video_document({"_id": "legacy", "s3_url": "https://old.example/video.mp4",
                                        "poster_url": "https://old.example/poster.jpg"})
        self.assertEqual(legacy["gallery_image_urls"], [])
        self.assertEqual(legacy["poster_url"], "https://old.example/poster.jpg")

    def test_gridfs_thumbnail_can_be_embedded(self):
        with patch("app.routes.fs") as fs:
            fs.get.return_value.read.return_value = b"jpeg"
            response = self.client.get("/thumbnail/000000000000000000000001")
        self.assertEqual(response.data, b"jpeg")
        self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "cross-origin")

    def test_poster_bytes_are_served_without_redirect(self):
        obj = MagicMock()
        stream = io.BytesIO(b"jpeg")
        obj.get_object.return_value = {"Body": StreamingBody(stream, 4), "ContentLength": 4}
        with patch("app.stream_routes.read_location", return_value=(obj, "historical")):
            response = self.client.get("/stream/example/poster.jpg")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"jpeg")
            self.assertEqual(response.mimetype, "image/jpeg")
            self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "cross-origin")
            response.close()
        obj.get_object.assert_called_once_with(Bucket="historical", Key="videos/example/poster.jpg")
        self.assertTrue(stream.closed)

    def test_missing_poster_returns_404(self):
        obj = MagicMock()
        obj.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        with patch("app.stream_routes.read_location", return_value=(obj, "current")):
            self.assertEqual(self.client.get("/stream/example/poster.jpg").status_code, 404)
