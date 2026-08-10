#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""file_service 核心流程单测: 上传校验、格式转换集成、后端调用、异常回滚。"""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PIL import Image

import tg_imagebed.config as config
import tg_imagebed.database.connection as db_connection
import tg_imagebed.database as database
from tg_imagebed.database import init_database, init_system_settings, update_system_setting
from tg_imagebed.services import file_service
from tg_imagebed.utils import convert_image_format


def _patch_db_path(testcase, tmp_root):
    """按项目现有测试模式隔离数据库(临时目录 + patch 三个模块的 DATABASE_PATH)。"""
    db_path = str(Path(tmp_root) / "test.db")
    patchers = [
        patch.object(config, "DATABASE_PATH", db_path),
        patch.object(db_connection, "DATABASE_PATH", db_path),
        patch.object(database, "DATABASE_PATH", db_path, create=True),
    ]
    for p in patchers:
        p.start()
        testcase.addCleanup(p.stop)
    return db_path


def _make_image(fmt: str = 'JPEG', size=(64, 48), **save_kwargs) -> bytes:
    """生成一张测试图片字节流。"""
    buf = io.BytesIO()
    img = Image.new('RGB', size, (200, 100, 50))
    img.save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


class ConvertImageFormatTests(unittest.TestCase):
    """convert_image_format: 开关/格式/动图/失败回退。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        _patch_db_path(self, self.temp_dir.name)
        init_database()
        init_system_settings()
        update_system_setting('image_conversion_enabled', '1')
        update_system_setting('image_conversion_format', 'webp')
        update_system_setting('image_conversion_quality', '80')

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_jpeg_to_webp(self):
        """JPEG → WebP: 返回新字节且可被 Pillow 打开。"""
        result = convert_image_format(_make_image('JPEG'), 'image/jpeg')
        self.assertIsNotNone(result)
        content, ext, mime = result
        self.assertEqual(ext, '.webp')
        self.assertEqual(mime, 'image/webp')
        with Image.open(io.BytesIO(content)) as img:
            self.assertEqual(img.format, 'WEBP')

    def test_disabled_returns_none(self):
        """转换开关关闭 → 返回 None (原图直传)。"""
        update_system_setting('image_conversion_enabled', '0')
        self.assertIsNone(convert_image_format(_make_image('JPEG'), 'image/jpeg'))

    def test_same_format_returns_none(self):
        """目标格式 == 原格式 → 不转换。"""
        update_system_setting('image_conversion_format', 'jpeg')
        self.assertIsNone(convert_image_format(_make_image('JPEG'), 'image/jpeg'))

    def test_invalid_mime_returns_none(self):
        """未知 MIME → 返回 None (不炸)。"""
        # application/octet-stream 是未知源格式, convert 内部会查不到映射而返回 None
        self.assertIsNone(convert_image_format(_make_image('JPEG'), 'application/octet-stream'))

    def test_animated_apng_skipped(self):
        """动图(多帧APNG)必须跳过, 绝不转换。"""
        buf = io.BytesIO()
        frames = [Image.new('RGB', (32, 32), c) for c in [(255, 0, 0), (0, 255, 0)]]
        frames[0].save(buf, format='PNG', save_all=True, append_images=frames[1:],
                       duration=100, loop=0)
        apng = buf.getvalue()
        # 确认确实是多帧动图, 且源格式是 png(可被转换映射识别, 才会走到动图分支)
        with Image.open(io.BytesIO(apng)) as img:
            self.assertTrue(getattr(img, 'is_animated', False))
        self.assertIsNone(convert_image_format(apng, 'image/png'))

    def test_rgba_png_to_webp_keeps_alpha(self):
        """透明 PNG → WebP: alpha 保留。"""
        buf = io.BytesIO()
        img = Image.new('RGBA', (32, 32), (255, 0, 0, 128))
        img.save(buf, format='PNG')
        result = convert_image_format(buf.getvalue(), 'image/png')
        self.assertIsNotNone(result)
        with Image.open(io.BytesIO(result[0])) as out:
            self.assertEqual(out.format, 'WEBP')
            self.assertIn('A', out.mode)  # 有 alpha 通道

    def test_corrupt_image_returns_none(self):
        """损坏图片 → 返回 None, 上传不因转换失败。"""
        self.assertIsNone(convert_image_format(b'\x00\x01\x02 not an image', 'image/jpeg'))


class ProcessUploadTests(unittest.TestCase):
    """process_upload: 校验、转换集成、后端调用、回滚。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        _patch_db_path(self, self.temp_dir.name)
        init_database()
        init_system_settings()

        # 默认存储后端指向临时 local 目录, 避免真实 Telegram 调用
        self.storage_root = Path(self.temp_dir.name) / 'storage'
        self.storage_root.mkdir()
        update_system_setting('storage_active_backend', 'local')
        update_system_setting(
            'storage_config_json',
            json.dumps({
                "active": "local",
                "backends": {
                    "local": {"driver": "local", "root_dir": str(self.storage_root)},
                },
            }),
        )
        # 重置存储路由缓存(全局 _router TTL 5s, 测试间必须隔离, 否则复用旧 temp_dir)
        self._reset_storage_router()

    def _reset_storage_router(self):
        """清空全局存储路由缓存。setUp+tearDown 都调用, 保证不污染后续测试
        (否则 5s TTL 内后续测试会复用本测试创建的 local 配置 router,
        导致 smoke 测试 /api/health 503: active=telegram 但 backends 无 telegram)。"""
        import tg_imagebed.storage.router as router_mod
        router_mod._router = None
        router_mod._router_ts = 0

    def tearDown(self):
        self._reset_storage_router()
        self.temp_dir.cleanup()

    def test_both_content_and_path_rejected(self):
        """file_content 和 staged_file_path 同时提供 → ValueError。"""
        with self.assertRaises(ValueError):
            file_service.process_upload(b'x', 'a.png', 'image/png',
                                        staged_file_path='/tmp/whatever')

    def test_neither_content_nor_path_rejected(self):
        """两个都没有 → ValueError。"""
        with self.assertRaises(ValueError):
            file_service.process_upload(None, 'a.png', 'image/png')

    def test_oversize_rejected(self):
        """超过大小限制 → None。"""
        update_system_setting('max_file_size_mb', '1')
        big = b'\xff\xd8\xff\xe0' + b'\x00' * (2 * 1024 * 1024)  # 2MB 假 JPEG 头
        result = file_service.process_upload(big, 'big.jpg', 'image/jpeg')
        self.assertIsNone(result)

    def test_invalid_image_rejected(self):
        """非图片字节 → None (魔数校验拒绝)。"""
        result = file_service.process_upload(b'plain text, not an image', 'f.txt', 'text/plain')
        self.assertIsNone(result)

    def test_upload_success_writes_local_file(self):
        """正常上传: local 后端落盘 + 返回 encrypted_id。"""
        result = file_service.process_upload(
            _make_image('JPEG'), 'photo.jpg', 'image/jpeg', username='tester'
        )
        self.assertIsNotNone(result)
        self.assertIn('encrypted_id', result)
        self.assertEqual(result['mime_type'], 'image/jpeg')
        # 文件确实写入了 storage 目录
        files = list(self.storage_root.rglob('*'))
        self.assertTrue(any(f.is_file() for f in files), 'local 后端应写入文件')

    def test_upload_with_conversion_changes_mime(self):
        """开启转换: JPEG → WebP, 落盘文件是 webp。"""
        update_system_setting('image_conversion_enabled', '1')
        update_system_setting('image_conversion_format', 'webp')
        result = file_service.process_upload(
            _make_image('JPEG'), 'photo.jpg', 'image/jpeg', username='tester'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['mime_type'], 'image/webp')
        self.assertTrue(result['filename'].endswith('.webp'))
        # 落盘文件可被 Pillow 打开且是 WEBP
        files = [f for f in self.storage_root.rglob('*') if f.is_file()]
        self.assertTrue(files)
        with Image.open(files[0]) as img:
            self.assertEqual(img.format, 'WEBP')

    def test_backend_failure_releases_reservation(self):
        """后端抛异常 → reservation 释放 + 异常上抛。"""
        fake_backend = MagicMock()
        fake_backend.put_bytes.side_effect = RuntimeError('backend down')
        router = MagicMock()
        router.resolve_upload_backend.return_value = 'local'
        router.get_backend.return_value = fake_backend
        release = MagicMock()

        with patch.object(file_service, 'get_storage_router', return_value=router), \
             patch.object(file_service, 'release_upload_reservation', release):
            with self.assertRaises(RuntimeError):
                file_service.process_upload(
                    _make_image('JPEG'), 'a.jpg', 'image/jpeg',
                    reservation_key='resv-1',
                )
        release.assert_called_once_with('resv-1')

    def test_save_failure_rolls_back_storage(self):
        """保存数据库失败 → 删除已存储对象 + 释放 reservation。"""
        fake_backend = MagicMock()
        fake_backend.put_bytes.return_value = MagicMock(
            file_id='fid', file_path='path', storage_key='key',
            file_size=100, storage_backend='local', storage_meta=None,
        )
        fake_backend.delete.return_value = True
        router = MagicMock()
        router.resolve_upload_backend.return_value = 'local'
        router.get_backend.return_value = fake_backend
        release = MagicMock()

        with patch.object(file_service, 'get_storage_router', return_value=router), \
             patch.object(file_service, 'save_file_info', side_effect=RuntimeError('db down')), \
             patch.object(file_service, 'release_upload_reservation', release):
            result = file_service.process_upload(
                _make_image('JPEG'), 'a.jpg', 'image/jpeg', reservation_key='resv-2'
            )
        self.assertIsNone(result)
        fake_backend.delete.assert_called_once_with(storage_key='key')
        release.assert_called_once_with('resv-2')


if __name__ == '__main__':
    unittest.main()
