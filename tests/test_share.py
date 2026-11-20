import unittest
from html.parser import HTMLParser
from unittest.mock import Mock, patch

from app import app
from app.config import API_PUBLIC_BASE_URL, STREAM_PRESIGN_EXPIRY


class Preview(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = {}
        self.watch = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and "property" in attrs:
            self.tags[attrs["property"]] = attrs["content"]
        if tag == "a" and attrs.get("id") == "watch":
            self.watch = attrs["href"]


class ShareTests(unittest.TestCase):
    def setUp(self):
        settings = patch.dict(app.config, TESTING=True)
        settings.start()
        self.addCleanup(settings.stop)
        self.client = app.test_client()
        self.video = {"_id": "stored-id", "short_id": "short", "title": 'Final <round> & "win"',
                      "resolution": "720x1280", "storage_prefix": "videos/stored-id"}

    def preview(self, doc):
        with patch("app.video_metadata.get_single_document", side_effect=[None, doc]) as lookup:
            response = self.client.get("/share?id=short")
            self.assertEqual(lookup.call_args.args, ({"short_id": "short"},))
        return response, Preview(response.get_data(as_text=True))

    def test_preview_contains_playable_media_before_javascript(self):
        response, preview = self.preview(self.video)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(preview.tags["og:title"], self.video["title"])
        self.assertTrue(preview.watch.endswith("/video.html?id=short&play=1"))
        self.assertTrue(preview.tags["og:url"].endswith("/video.html?id=short"))
        self.assertEqual(preview.tags["og:video"], f"{API_PUBLIC_BASE_URL}/stream/stored-id/original.mp4")
        self.assertEqual(preview.tags["og:video:type"], "video/mp4")
        self.assertEqual(preview.tags["og:video:width"], "720")
        self.assertEqual(preview.tags["og:video:height"], "1280")
        self.assertTrue(preview.tags["og:image"].endswith("/stream/stored-id/poster.jpg"))

    def test_legacy_media_and_gridfs_thumbnail(self):
        url = "https://storage.test/replay.WebM?version=1"
        _, preview = self.preview({"_id": "old", "s3_url": url, "thumbnail_id": "abc"})
        self.assertEqual(preview.tags["og:video"], url)
        self.assertEqual(preview.tags["og:video:type"], "video/webm")
        self.assertTrue(preview.tags["og:image"].endswith("/thumbnail/abc"))

    def test_unready_or_playlist_only_replays_have_no_video_embed(self):
        for status in ("pending", "processing", "failed"):
            response, preview = self.preview(dict(self.video, encoding_status=status))
            self.assertNotIn("og:video", preview.tags)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        _, preview = self.preview({"_id": "old", "s3_url": "https://storage.test/replay.m3u8"})
        self.assertNotIn("og:video", preview.tags)
        poster = self.client.get("/static/replay-poster.png")
        try:
            self.assertEqual(poster.mimetype, "image/png")
            self.assertTrue(poster.data.startswith(b"\x89PNG"))
        finally:
            poster.close()
        self.assertEqual(self.preview(None)[0].status_code, 404)

    def test_inline_get_and_head_sign_the_matching_storage_operation(self):
        store = Mock()
        store.generate_presigned_url.return_value = "https://storage.test/signed"
        with patch("app.storage.read_location", return_value=(store, "historical")):
            for method, operation in (("GET", "get_object"), ("HEAD", "head_object")):
                response = self.client.open("/stream/stored-id/original.mp4", method=method,
                                            headers={"Range": "bytes=0-1"})
                self.assertEqual(response.location, "/stream/stored-id/inline")
                self.assertEqual(response.status_code, 307)
                response = self.client.open(response.location, method=method,
                                            headers={"Range": "bytes=0-1"})
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.location, "https://storage.test/signed")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                store.generate_presigned_url.assert_called_with(
                    operation, Params={"Bucket": "historical", "Key": "videos/stored-id/original.mp4"},
                    ExpiresIn=STREAM_PRESIGN_EXPIRY)
