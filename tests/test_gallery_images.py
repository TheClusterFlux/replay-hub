"""Poster responses must be readable from the cross-origin isolated gallery."""
import io
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from app import app


class GalleryImageTests(unittest.TestCase):
    def setUp(self):
        settings = patch.dict(app.config, TESTING=True)
        settings.start()
        self.addCleanup(settings.stop)
        self.client = app.test_client()

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
