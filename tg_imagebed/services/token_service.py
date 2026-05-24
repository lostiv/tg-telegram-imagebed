#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Token 统一调度层 — 级联删除 / 影响范围查询 / 批量操作
"""
import sqlite3
from typing import Any, Dict, List, Optional

from ..config import logger
from ..database import (
    admin_create_token,
    admin_delete_token,
    get_system_setting,
)
from ..database.connection import get_connection


class TokenService:
    """Token 管理统一入口，封装级联清理与批量操作逻辑。"""

    # ── 创建 ──────────────────────────────────────────────
    @staticmethod
    def create_token(
        *,
        description: Optional[str] = None,
        expires_at: Any = None,
        upload_limit: int = 100,
        is_active: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """统一创建 Token，委托给 database 层。"""
        return admin_create_token(
            description=description,
            expires_at=expires_at,
            upload_limit=upload_limit,
            is_active=is_active,
        )

    # ── 删除关联图片（内部辅助） ────────────────────────────
    @staticmethod
    def _collect_image_rows_for_token_str(token_str: str, cursor) -> List[Dict[str, Any]]:
        """查询指定 token 关联的图片行。"""
        cursor.execute(
            "SELECT encrypted_id, file_size, storage_backend, storage_key, "
            "group_chat_id, group_message_id, storage_meta "
            "FROM file_storage WHERE auth_token = ?",
            (token_str,),
        )
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _delete_image_records_for_token_str(
        token_str: str,
        cursor,
        image_rows: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """仅删除数据库记录并修正 token 计数，不执行外部 I/O。"""
        result = {"images_deleted": 0, "tg_deleted": 0}
        encrypted_ids = [row["encrypted_id"] for row in image_rows if row.get("encrypted_id")]
        if not encrypted_ids:
            return result

        def _chunked(seq, size=900):
            for i in range(0, len(seq), size):
                yield seq[i:i + size]

        for chunk in _chunked(encrypted_ids):
            cursor.executemany(
                "DELETE FROM file_storage WHERE encrypted_id = ?",
                ((encrypted_id,) for encrypted_id in chunk),
            )
            if cursor.rowcount > 0:
                result["images_deleted"] += cursor.rowcount

        if result["images_deleted"] > 0:
            cursor.execute(
                "UPDATE auth_tokens SET upload_count = MAX(0, upload_count - ?) WHERE token = ?",
                (result["images_deleted"], token_str),
            )

        return result

    @staticmethod
    def _delete_external_files(image_rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """删除存储后端文件和 Telegram 消息。应在数据库事务外调用。"""
        import json as _json
        import requests as http_requests

        result = {"tg_deleted": 0}
        if not image_rows:
            return result

        # 检查是否启用 TG 同步删除
        tg_sync_delete_enabled = str(get_system_setting('tg_sync_delete_enabled') or '1') == '1'

        # 获取存储路由器（用于删除存储后端文件）
        from ..storage.router import get_storage_router
        router = get_storage_router()

        # 获取 bot_token（用于 TG 消息删除）
        bot_token = None
        if tg_sync_delete_enabled:
            try:
                from ..bot_control import get_effective_bot_token
                bot_token, _ = get_effective_bot_token()
            except Exception:
                pass

        tg_seen = set()

        for file_row in image_rows:
            encrypted_id = file_row['encrypted_id']
            storage_backend = (file_row.get('storage_backend') or 'telegram').strip()
            storage_key = file_row.get('storage_key') or ''

            # 删除存储后端文件（静默忽略失败）
            if storage_key:
                try:
                    backend = router.get_backend(storage_backend)
                    backend.delete(storage_key=storage_key)
                except Exception as e:
                    logger.debug(f"删除存储文件失败: {encrypted_id}, {e}")

            # TG 消息同步删除
            if tg_sync_delete_enabled and bot_token:
                chat_id = file_row.get('group_chat_id')
                message_id = file_row.get('group_message_id')

                # 兼容历史数据：从 storage_meta 中提取 message_id，从后端配置获取 chat_id
                if not message_id or not chat_id:
                    try:
                        meta_raw = file_row.get('storage_meta')
                        meta = {}
                        if isinstance(meta_raw, str) and meta_raw.strip():
                            parsed = _json.loads(meta_raw)
                            if isinstance(parsed, dict):
                                meta = parsed
                        elif isinstance(meta_raw, dict):
                            meta = meta_raw
                        if not message_id:
                            message_id = meta.get('message_id')
                        if not chat_id and storage_backend:
                            try:
                                be = router.get_backend(storage_backend)
                                if hasattr(be, '_chat_id'):
                                    chat_id = be._chat_id
                            except Exception:
                                pass
                    except Exception:
                        pass

                if chat_id and message_id:
                    key = (chat_id, message_id)
                    if key not in tg_seen:
                        tg_seen.add(key)
                        try:
                            resp = http_requests.post(
                                f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                                data={'chat_id': chat_id, 'message_id': message_id},
                                timeout=5,
                            )
                            if resp.ok and resp.json().get('ok'):
                                result["tg_deleted"] += 1
                        except Exception:
                            pass

        return result

    @staticmethod
    def _try_delete_external_files(image_rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """Best-effort external cleanup after database changes have committed."""
        try:
            return TokenService._delete_external_files(image_rows)
        except Exception as e:
            logger.warning(f"Token 关联图片外部清理失败，数据库删除已完成: {e}")
            return {"tg_deleted": 0}

    @staticmethod
    def _delete_images_for_token_str(token_str: str, cursor) -> Dict[str, int]:
        """
        删除指定 token 关联的数据库图片记录。

        兼容旧调用；外部存储清理应在事务提交后调用 _try_delete_external_files。
        """
        image_rows = TokenService._collect_image_rows_for_token_str(token_str, cursor)
        result = TokenService._delete_image_records_for_token_str(token_str, cursor, image_rows)

        return result

    # ── 级联删除 ──────────────────────────────────────────
    @staticmethod
    def delete_token(token_id: int, *, delete_images: bool = False) -> bool:
        """
        级联删除 Token：
        1. （可选）删除关联图片（存储后端 + 数据库记录）
        2. file_storage.auth_token 置空（仅在不删除图片时）
        3. galleries.owner_token 置空（owner_type='token' 的画集）
        4. gallery_token_access 清理
        5. 删除 auth_tokens 记录
        """
        try:
            image_rows: List[Dict[str, Any]] = []
            with get_connection() as conn:
                cursor = conn.cursor()

                # 先查出 token 字符串
                cursor.execute(
                    "SELECT token FROM auth_tokens WHERE rowid = ?",
                    (int(token_id),),
                )
                row = cursor.fetchone()
                if not row:
                    return False

                token_str = row[0]

                # 可选：删除关联图片
                if delete_images:
                    image_rows = TokenService._collect_image_rows_for_token_str(token_str, cursor)
                    TokenService._delete_image_records_for_token_str(token_str, cursor, image_rows)
                else:
                    # 仅置空 auth_token
                    cursor.execute(
                        "UPDATE file_storage SET auth_token = NULL WHERE auth_token = ?",
                        (token_str,),
                    )

                # galleries.owner_token 置空
                cursor.execute(
                    "UPDATE galleries SET owner_token = NULL WHERE owner_token = ?",
                    (token_str,),
                )
                # gallery_token_access 清理
                cursor.execute(
                    "DELETE FROM gallery_token_access WHERE token = ?",
                    (token_str,),
                )
                # 删除 auth_tokens
                cursor.execute(
                    "DELETE FROM auth_tokens WHERE rowid = ?",
                    (int(token_id),),
                )

            action = "级联删除（含图片）" if delete_images else "级联删除"
            if delete_images:
                TokenService._try_delete_external_files(image_rows)
            logger.info(f"TokenService {action} Token: ID={token_id}")
            return True

        except sqlite3.OperationalError:
            raise
        except Exception as e:
            logger.error(f"TokenService 级联删除 Token 失败: {e}")
            return False

    # ── 影响范围查询 ──────────────────────────────────────
    @staticmethod
    def get_token_impact(token_id: int) -> Optional[Dict[str, Any]]:
        """查询删除该 Token 的影响范围。返回 None 表示 Token 不存在。"""
        try:
            with get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute(
                    "SELECT token FROM auth_tokens WHERE rowid = ?",
                    (int(token_id),),
                )
                row = cursor.fetchone()
                if not row:
                    return None

                token_str = row[0]

                cursor.execute(
                    "SELECT COUNT(1) FROM file_storage WHERE auth_token = ?",
                    (token_str,),
                )
                upload_count = cursor.fetchone()[0] or 0

                cursor.execute(
                    "SELECT COUNT(1) FROM galleries WHERE owner_token = ?",
                    (token_str,),
                )
                gallery_count = cursor.fetchone()[0] or 0

                cursor.execute(
                    "SELECT COUNT(1) FROM gallery_token_access WHERE token = ?",
                    (token_str,),
                )
                access_count = cursor.fetchone()[0] or 0

            return {
                "upload_count": upload_count,
                "gallery_count": gallery_count,
                "access_count": access_count,
            }

        except Exception as e:
            logger.error(f"TokenService 查询影响范围失败: {e}")
            raise

    # ── 批量操作 ──────────────────────────────────────────
    @staticmethod
    def batch_update_status(token_ids: List[int], is_active: bool) -> Dict[str, int]:
        """批量启用/禁用，返回 {success_count, fail_count}。"""
        success = 0
        fail = 0
        active_val = 1 if is_active else 0
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                for tid in token_ids:
                    try:
                        cursor.execute(
                            "UPDATE auth_tokens SET is_active = ? WHERE rowid = ?",
                            (active_val, int(tid)),
                        )
                        if cursor.rowcount > 0:
                            success += 1
                        else:
                            fail += 1
                    except Exception:
                        fail += 1
        except Exception as e:
            logger.error(f"TokenService 批量更新状态失败: {e}")
            raise
        status_text = "启用" if is_active else "禁用"
        logger.info(f"TokenService 批量{status_text}: 成功={success}, 失败={fail}")
        return {"success_count": success, "fail_count": fail}

    @staticmethod
    def batch_delete(token_ids: List[int], *, delete_images: bool = False) -> Dict[str, int]:
        """批量级联删除，返回 {success_count, fail_count, images_deleted, tg_deleted}。"""
        success = 0
        fail = 0
        total_images_deleted = 0
        total_tg_deleted = 0
        external_rows: List[Dict[str, Any]] = []
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                for tid in token_ids:
                    try:
                        cursor.execute(
                            "SELECT token FROM auth_tokens WHERE rowid = ?",
                            (int(tid),),
                        )
                        row = cursor.fetchone()
                        if not row:
                            fail += 1
                            continue
                        token_str = row[0]

                        # 可选：删除关联图片
                        if delete_images:
                            image_rows = TokenService._collect_image_rows_for_token_str(token_str, cursor)
                            img_result = TokenService._delete_image_records_for_token_str(token_str, cursor, image_rows)
                            external_rows.extend(image_rows)
                            total_images_deleted += img_result["images_deleted"]
                        else:
                            cursor.execute(
                                "UPDATE file_storage SET auth_token = NULL WHERE auth_token = ?",
                                (token_str,),
                            )

                        cursor.execute(
                            "UPDATE galleries SET owner_token = NULL WHERE owner_token = ?",
                            (token_str,),
                        )
                        cursor.execute(
                            "DELETE FROM gallery_token_access WHERE token = ?",
                            (token_str,),
                        )
                        cursor.execute(
                            "DELETE FROM auth_tokens WHERE rowid = ?",
                            (int(tid),),
                        )
                        success += 1
                    except Exception:
                        fail += 1
        except Exception as e:
            logger.error(f"TokenService 批量删除失败: {e}")
            raise
        action = "批量删除（含图片）" if delete_images else "批量删除"
        if delete_images:
            external_result = TokenService._try_delete_external_files(external_rows)
            total_tg_deleted = external_result.get("tg_deleted", 0)
        logger.info(f"TokenService {action}: 成功={success}, 失败={fail}")
        result = {"success_count": success, "fail_count": fail}
        if delete_images:
            result["images_deleted"] = total_images_deleted
            result["tg_deleted"] = total_tg_deleted
        return result

    @staticmethod
    def batch_get_impact(token_ids: List[int]) -> Dict[str, Any]:
        """批量影响范围汇总。"""
        total_uploads = 0
        total_galleries = 0
        total_access = 0
        for tid in token_ids:
            impact = TokenService.get_token_impact(tid)
            if impact:
                total_uploads += impact["upload_count"]
                total_galleries += impact["gallery_count"]
                total_access += impact["access_count"]
        return {
            "token_count": len(token_ids),
            "upload_count": total_uploads,
            "gallery_count": total_galleries,
            "access_count": total_access,
        }

    # ── 用户侧删除（按 token 字符串） ─────────────────────
    @staticmethod
    def delete_token_by_string(token: str, *, delete_images: bool = False) -> bool:
        """
        按 token 字符串级联删除（用户侧删除），可选同时删除关联图片。

        级联：（可选）删除图片 → file_storage.auth_token 置空
              → galleries.owner_token 置空 → gallery_token_access 清理
              → auth_tokens 删除
        """
        token = (token or '').strip()
        if not token:
            return False
        try:
            image_rows: List[Dict[str, Any]] = []
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM auth_tokens WHERE token = ?", (token,))
                if not cursor.fetchone():
                    return False

                # 可选：删除关联图片
                if delete_images:
                    image_rows = TokenService._collect_image_rows_for_token_str(token, cursor)
                    TokenService._delete_image_records_for_token_str(token, cursor, image_rows)
                else:
                    cursor.execute(
                        "UPDATE file_storage SET auth_token = NULL WHERE auth_token = ?",
                        (token,),
                    )

                cursor.execute(
                    "UPDATE galleries SET owner_token = NULL WHERE owner_token = ?",
                    (token,),
                )
                cursor.execute(
                    "DELETE FROM gallery_token_access WHERE token = ?",
                    (token,),
                )
                cursor.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))

            action = "用户侧级联删除（含图片）" if delete_images else "用户侧级联删除"
            if delete_images:
                TokenService._try_delete_external_files(image_rows)
            logger.info(f"{action} Token: {token[:20]}...")
            return True
        except Exception as e:
            logger.error(f"用户侧删除 Token 失败: {e}")
            return False
