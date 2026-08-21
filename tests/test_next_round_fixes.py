#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

requests_stub = ModuleType("requests")
requests_stub.Session = Mock
sys.modules.setdefault("requests", requests_stub)

from tg_imagebed.database.gallery_home import _query_gallery_items
from tg_imagebed.storage.backends.telegram import TelegramBackend


class GalleryHomeFixTests(unittest.TestCase):
    def test_query_gallery_items_validates_order_and_applies_limit(self):
        cursor = Mock()
        cursor.fetchall.return_value = []

        _query_gallery_items(
            cursor,
            order_clause="g.updated_at DESC; DROP TABLE galleries",
            limit=1000,
        )

        sql, params = cursor.execute.call_args.args
        self.assertNotIn("DROP TABLE", sql)
        self.assertIn("ORDER BY g.updated_at DESC", sql)
        self.assertIn("LIMIT ?", sql)
        self.assertEqual(params[-1], 1000)


class TelegramDownloadFixTests(unittest.TestCase):
    def setUp(self):
        self.backend = TelegramBackend(name="telegram", bot_token="token", chat_id=123456)

    def test_non_success_retry_response_is_closed(self):
        first_resp = Mock(status_code=500)
        second_resp = Mock(status_code=503)
        self.backend._session.get = Mock(side_effect=[first_resp, second_resp])

        with patch.object(self.backend, "_is_file_path_cache_expired", return_value=True), \
                patch.object(self.backend, "_get_file_path", return_value="fresh/path"):
            result = self.backend.download(
                file_info={
                    "storage_key": "file-id",
                    "file_path": "old/path",
                    "file_size": 1,
                },
                range_header=None,
            )

        first_resp.close.assert_called_once_with()
        second_resp.close.assert_called_once_with()
        self.assertEqual(result.status_code, 503)

    def test_stream_body_close_releases_response(self):
        resp = Mock(status_code=200)
        resp.headers = {"content-length": "12"}
        resp.iter_content.return_value = iter([b"first", b"second"])
        self.backend._session.get = Mock(return_value=resp)

        with patch.object(self.backend, "_should_use_kurigram_download", return_value=False):
            result = self.backend.download(
                file_info={
                    "storage_key": "file-id",
                    "file_path": "file/path",
                    "file_size": 1,
                },
                range_header=None,
            )

        body = result.body
        self.assertEqual(next(body), b"first")
        body.close()
        resp.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
