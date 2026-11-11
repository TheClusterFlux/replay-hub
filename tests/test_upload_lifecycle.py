"""Regression checks for recovery and deletion boundaries."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.encoding_queue import discard, recover_receiving
from app.upload_cleanup import clean_session


class UploadLifecycleTests(unittest.TestCase):
    def test_recovery_requires_matching_catalog_ticket(self):
        accepted = {"video_id": "one", "token": "stored"}
        incomplete = {"video_id": "two", "token": "unfinished"}
        with patch("app.encoding_queue.slots") as slots, patch("app.encoding_queue.collection") as catalog:
            slots.find.return_value = [accepted, incomplete]
            catalog.find_one.side_effect = [{"_id": "one"}, None]
            recover_receiving()
            self.assertEqual(catalog.find_one.call_args_list[1].args[0],
                             {"_id": "two", "encoding_ticket": "unfinished"})
            slots.update_one.assert_called_once_with({"token": "stored", "state": "receiving"},
                                                     {"$set": {"state": "pending"}})

    def test_expired_chunks_can_only_delete_their_own_directory(self):
        with tempfile.TemporaryDirectory() as root, patch("app.encoding_queue.UPLOAD_FOLDER", root), \
             patch("app.encoding_queue.slots"):
            path = Path(root) / "chunks_account_file"
            path.mkdir()
            (path / "metadata.json").write_text(json.dumps({"ticket": "new"}))
            ticket = {"expired": True, "local_dir": path.name, "token": "old"}
            discard(ticket)
            self.assertTrue(path.is_dir())
            discard({**ticket, "token": "new"})
            self.assertFalse(path.exists())

    def test_referenced_or_active_video_cannot_be_purged(self):
        with patch("app.upload_cleanup.get_db"), patch("app.upload_cleanup.referenced") as referenced, \
             patch("app.upload_cleanup.slots") as slots, patch("app.upload_cleanup.purge_prefix") as purge:
            session = {"_id": "replay", "state": "deleting", "updated": 1}
            referenced.return_value = True
            clean_session(session, now=10)
            referenced.return_value = False
            slots.find_one.return_value = {"state": "processing"}
            clean_session(session, now=10)
            purge.assert_not_called()

    def test_late_put_sweep_retries_partial_deletion(self):
        session = {"_id": "replay", "state": "deleting", "purged": True, "purge_after": 20}
        with patch("app.upload_cleanup.get_db") as db, patch("app.upload_cleanup.referenced", return_value=False), \
             patch("app.upload_cleanup.slots") as slots, patch("app.upload_cleanup.purge_prefix") as purge:
            slots.find_one.return_value = None
            clean_session(session, now=19)
            purge.assert_not_called()
            purge.side_effect = OSError("object storage offline")
            with self.assertRaises(OSError):
                clean_session(session, now=21)
            sessions = db.return_value.hls_uploads
            sessions.delete_one.assert_not_called()
            purge.side_effect = None
            clean_session(session, now=22)
            purge.assert_called_with("videos/replay/")
            sessions.delete_one.assert_called_once_with({"_id": "replay", "state": "deleting"})
