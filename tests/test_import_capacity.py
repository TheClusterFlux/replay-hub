"""Exercise URL validation before a socket is opened and capacity estimates before upload."""
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, ServiceUnavailable

from app.encoding_capacity import check_capacity, check_memory, check_space, MIB
from app.url_import import open_origin, download_url


class ImportTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, URL_IMPORT_HOSTS="media.test")
        env.start()
        self.addCleanup(env.stop)

    def test_private_answer_blocks_entire_mixed_dns_result(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1"):
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=[
                (0, 0, 0, "", ("1.1.1.1", 443)), (0, 0, 0, "", (ip, 443))
            ]), patch("socket.create_connection") as connect:
                with self.assertRaises(BadRequest):
                    open_origin("https://media.test/video.mp4", 30)
                connect.assert_not_called()

    def test_connect_to_validated_ip_with_original_tls_hostname(self):
        with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("1.1.1.1", 443))]) as dns, \
             patch("socket.create_connection") as connect, patch("ssl.create_default_context") as tls, \
             patch("http.client.HTTPSConnection") as https:
            open_origin("https://media.test/one.mp4?sig=abc", 30)
            dns.assert_called_once_with("media.test", 443, type=socket.SOCK_STREAM)
            connect.assert_called_once_with(("1.1.1.1", 443), timeout=10)
            self.assertEqual(tls.return_value.wrap_socket.call_args.kwargs["server_hostname"], "media.test")
            https.return_value.request.assert_called_once_with("GET", "/one.mp4?sig=abc", headers={"Accept-Encoding": "identity"})

    def test_redirect_revalidates_host_before_connecting(self):
        response = MagicMock(status=302)
        response.getheader.return_value = "https://169.254.169.254/credentials"
        with patch("app.url_import.open_origin", side_effect=[(MagicMock(), response), BadRequest()]) as opener:
            with self.assertRaises(BadRequest):
                download_url("https://media.test/video", "unused", 1024)
            self.assertEqual(opener.call_args.args[0], response.getheader.return_value)
        with patch("socket.create_connection") as connect, self.assertRaises(BadRequest):
            open_origin(response.getheader.return_value, 30)
        connect.assert_not_called()

    def test_lengthless_response_is_capped_during_streaming(self):
        response = MagicMock(status=200)
        response.getheader.return_value = None
        response.read1.side_effect = [b"12", b"345"]
        with tempfile.TemporaryDirectory() as directory, patch("app.url_import.open_origin", return_value=(MagicMock(), response)):
            target = Path(directory) / "source"
            with self.assertRaises(RequestEntityTooLarge):
                download_url("https://media.test/video", target, 4)
            self.assertEqual(target.read_bytes(), b"12")

    def test_disk_and_file_size_limits(self):
        with patch("app.encoding_capacity.shutil.disk_usage", return_value=SimpleNamespace(free=2*MIB)), \
             self.assertRaisesRegex(ServiceUnavailable, "disk space"):
            check_space(".", 3*MIB)
        with patch("app.encoding_capacity.SERVER_LIMIT", MIB), patch("shutil.which", return_value="ffmpeg"), \
             self.assertRaisesRegex(RequestEntityTooLarge, "1 MiB"):
            check_capacity(MIB+1, ".")

    def test_memory_headroom_accounts_for_reclaimable_cache(self):
        with patch.object(Path, "read_text", side_effect=[str(512*MIB), str(490*MIB), "inactive_file 0"]), \
             self.assertRaisesRegex(ServiceUnavailable, "low on memory"):
            check_memory()
        with patch.object(Path, "read_text", side_effect=[str(512*MIB), str(490*MIB), f"inactive_file {120*MIB}"]):
            check_memory()
