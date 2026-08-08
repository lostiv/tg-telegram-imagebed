#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 消息处理器模块

包含 /start 命令处理和图片上传处理。
"""
import asyncio
import os
import re
import time
from html import escape as html_escape
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update

from ..config import logger
from ..utils import format_size
from .media_batch import _MediaBatch, _media_group_batches, _flush_media_group, _MAX_BATCH_ITEMS
from .state import _inc_bot_stats, _inc_template_error

# 文件下载超时（秒）
_DOWNLOAD_TIMEOUT = 60


async def start(update: Update, context):
    """处理 /start 命令"""
    from ..database import get_stats, upsert_tg_user
    from ..utils import get_domain
    from .state import _get_bot_status

    # 记录用户到 tg_users 表
    user = update.effective_user
    if user:
        upsert_tg_user(
            tg_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )

    stats = get_stats()
    bot_status = _get_bot_status()
    web_url = html_escape(str(get_domain(None) or ''))
    await update.message.reply_text(
        "☁️ <b>Telegram 云图床机器人</b>\n\n"
        "✨ 直接发送图片获取永久直链\n\n"
        f"🌐 <b>Web界面:</b> {web_url}\n"
        f"📊 <b>已存储:</b> {stats['total_files']} 个文件\n"
        f"💾 <b>总大小:</b> {stats['total_size'] / 1024 / 1024:.1f} MB\n"
        f"🤖 <b>Bot统计:</b> 处理 {bot_status['stats_processed']} 张"
        f"（✅{bot_status['stats_success']} ❌{bot_status['stats_failed']}）\n\n"
        "直接发送图片即可开始使用！",
        parse_mode='HTML'
    )


def _parse_id_list(raw: str) -> set:
    """解析逗号分隔的 ID 列表"""
    if not raw:
        return set()
    try:
        return {int(x.strip()) for x in raw.split(',') if x.strip()}
    except ValueError:
        return set()


async def _check_group_permissions(update, get_system_setting, has_bound_tokens) -> tuple:
    """
    检查群组上传权限

    Returns:
        (allowed: bool, reply_enabled: bool, delete_delay: int)
    """
    from ..database import get_system_setting as _gs
    if str(_gs('group_upload_admin_only') or '0') == '1':
        admin_raw = str(_gs('group_admin_ids') or '').strip()
        admin_ids = _parse_id_list(admin_raw)
        user = update.effective_user
        if not user or not admin_ids or user.id not in admin_ids:
            return False, False, 0
    elif str(_gs('group_upload_tg_bound_only') or '0') == '1':
        user = update.effective_user
        if not user or not has_bound_tokens(user.id):
            return False, False, 0

    reply_enabled = str(_gs('group_upload_reply') or '1') == '1'
    try:
        delete_delay = max(0, int(_gs('group_upload_delete_delay') or '0'))
    except (ValueError, TypeError):
        delete_delay = 0
    return True, reply_enabled, delete_delay


async def _check_private_permissions(update, get_system_setting, has_bound_tokens) -> bool:
    """
    检查私聊上传权限，不通过时直接回复错误消息

    Returns:
        allowed: bool
    """
    from ..database import get_system_setting as _gs
    message = update.effective_message
    if str(_gs('bot_private_upload_enabled') or '1') != '1':
        await message.reply_text("❌ 私聊上传功能已关闭")
        return False

    mode = str(_gs('bot_private_upload_mode') or 'open').strip().lower()
    user = update.effective_user

    if mode == 'admin_only':
        admin_raw = str(_gs('bot_private_admin_ids') or '').strip()
        admin_ids = _parse_id_list(admin_raw)
        if not user or not admin_ids or user.id not in admin_ids:
            await message.reply_text("❌ 仅管理员可通过私聊上传")
            return False
    elif mode == 'tg_bound':
        if not user or not has_bound_tokens(user.id):
            await message.reply_text(
                "❌ 仅绑定 Token 的用户可通过私聊上传\n\n"
                "💡 请先使用 /login 登录 Web 端并生成 Token"
            )
            return False
    return True


def _extract_file_from_message(message, get_allowed_extensions, get_mime_type_fn):
    """
    从消息中提取文件信息

    Returns:
        (tg_file, filename, content_type, file_unique_id) 或 (None, ...) 表示无图片
    """
    tg_file = None
    filename = ""
    content_type = "image/jpeg"
    file_unique_id = None

    if message.photo:
        tg_file = message.photo[-1]
        file_unique_id = tg_file.file_unique_id
        filename = f"telegram_{file_unique_id}.jpg"
        content_type = "image/jpeg"
    elif message.document:
        doc = message.document
        mime = (doc.mime_type or "").lower()
        doc_name = (doc.file_name or "").lower()
        allowed = get_allowed_extensions()
        is_image = mime.startswith("image/") or any(
            doc_name.endswith(f'.{ext}') for ext in allowed
        )
        if is_image:
            tg_file = doc
            file_unique_id = doc.file_unique_id
            filename = doc.file_name or f"telegram_{file_unique_id}"
            content_type = doc.mime_type or get_mime_type_fn(filename)

    return tg_file, filename, content_type, file_unique_id


async def _resolve_upload_token(user, is_group: bool, get_active_user_tokens, get_default_upload_token, message) -> tuple:
    """
    解析上传关联 Token

    Returns:
        (token: Optional[str], should_return: bool)
        should_return=True 表示需要中断处理（多 Token 无默认的私聊场景）
    """
    if not user:
        return None, False

    active_tokens = get_active_user_tokens(user.id)
    if len(active_tokens) == 1:
        return active_tokens[0]['token'], False
    elif len(active_tokens) > 1:
        default = get_default_upload_token(user.id)
        if default:
            return default, False
        # 多 Token 无默认：私聊提示，群组静默使用回退
        if not is_group:
            await message.reply_text(
                "⚠️ 你有多个 Token，请先设置默认上传 Token\n\n"
                "使用 /settoken 选择默认 Token"
            )
            return None, True
    return None, False


def _build_reply_text(result: dict, permanent_url: str, filename: str, get_system_setting) -> tuple:
    """
    构建上传成功的回复文本

    Returns:
        (text: str, parse_mode: Optional[str])
    """
    reply_template = str(get_system_setting('bot_reply_template') or '').strip()
    show_size = str(get_system_setting('bot_reply_show_size') or '1') == '1'
    show_filename = str(get_system_setting('bot_reply_show_filename') or '0') == '1'
    strict_mode = str(get_system_setting('bot_template_strict_mode') or '0') == '1'

    if reply_template:
        try:
            text = reply_template.format(
                url=permanent_url,
                size=format_size(result['file_size']),
                filename=result.get('original_filename') or filename,
                id=result['encrypted_id'],
            )
            return text, None
        except Exception as e:
            _inc_template_error()
            logger.warning(f"Bot 回复模板渲染失败: {type(e).__name__}: {e}")
            if strict_mode:
                return "⚠️ 回复模板配置错误，请联系管理员", None

    from html import escape as html_escape
    lines = [
        "✅ <b>上传成功！</b>\n",
        f"🔗 <b>永久直链:</b>\n<code>{html_escape(permanent_url)}</code>\n",
    ]
    if show_filename:
        fname = html_escape(result.get('original_filename') or filename)
        lines.append(f"📄 <b>文件名:</b> {fname}")
    if show_size:
        lines.append(f"📊 <b>文件大小:</b> {format_size(result['file_size'])}")
    lines.append("💡 链接永久有效")
    return '\n'.join(lines), 'HTML'


async def handle_photo(update: Update, context):
    """处理图片上传（私聊/群组/频道）"""
    from ..services.file_service import process_upload, record_existing_telegram_file
    from ..utils import get_domain, get_image_domain, get_mime_type as _get_mime_type
    from ..database import get_system_setting, get_system_setting_int, has_bound_tokens
    from ..database import get_active_user_tokens, get_default_upload_token

    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    chat_type = (getattr(chat, 'type', '') or '').lower()
    is_group = chat_type in ('group', 'supergroup', 'channel')

    # 权限检查
    reply_enabled = True
    delete_delay = 0
    if is_group:
        allowed, reply_enabled, delete_delay = await _check_group_permissions(
            update, get_system_setting, has_bound_tokens
        )
        if not allowed:
            return
    else:
        if not await _check_private_permissions(update, get_system_setting, has_bound_tokens):
            return

    # 获取用户信息
    user = update.effective_user
    tg_user_id = user.id if user else None
    if user:
        username = user.username or user.full_name or str(user.id)
    else:
        username = getattr(chat, 'title', '') or 'channel'

    # 解析上传关联 Token
    upload_auth_token, should_return = await _resolve_upload_token(
        user, is_group, get_active_user_tokens, get_default_upload_token, message
    )
    if should_return:
        return

    # 检测批量上传（media_group_id）
    media_group_id = getattr(message, 'media_group_id', None)
    use_batch = bool(is_group and reply_enabled and media_group_id)

    # 发送处理中消息（批量模式下延迟到首张图片时发送）
    status_msg = None
    if reply_enabled and not use_batch:
        try:
            status_msg = await message.reply_text("⏳ 正在处理图片...")
        except Exception:
            pass

    try:
        # 提取图片信息
        from ..config import get_allowed_extensions
        tg_file, filename, content_type, file_unique_id = _extract_file_from_message(
            message, get_allowed_extensions, _get_mime_type
        )

        if not tg_file:
            if status_msg:
                await status_msg.edit_text("❌ 请发送图片文件")
            elif reply_enabled:
                try:
                    await message.reply_text("❌ 请发送图片文件")
                except Exception:
                    pass
            return

        max_size_mb = get_system_setting_int('max_file_size_mb', 100, minimum=1, maximum=1024)
        max_size_bytes = max_size_mb * 1024 * 1024
        file_size = getattr(tg_file, 'file_size', 0) or 0
        if file_size > max_size_bytes:
            if status_msg:
                await status_msg.edit_text(f"❌ 文件大小超过 {max_size_mb}MB 限制")
            return

        # Caption 自定义文件名
        if message.caption and str(get_system_setting('bot_caption_filename_enabled') or '1') == '1':
            from .commands import _sanitize_filename
            original_ext = os.path.splitext(filename)[1] or '.jpg'
            custom_name = _sanitize_filename(message.caption, original_ext)
            if custom_name:
                filename = custom_name

        # 批量模式：添加到累加器，延迟统一处理
        if use_batch:
            batch_key = (chat.id, str(media_group_id))
            batch = _media_group_batches.get(batch_key)
            if not batch:
                batch = _MediaBatch(
                    chat_id=chat.id,
                    media_group_id=str(media_group_id),
                    message_thread_id=getattr(message, 'message_thread_id', None),
                    delete_delay=delete_delay,
                )
                _media_group_batches[batch_key] = batch

            if len(batch.items) >= _MAX_BATCH_ITEMS:
                logger.warning(f"批量上传超过上限 {_MAX_BATCH_ITEMS}，忽略多余图片: chat={chat.id} group={media_group_id}")
                return

            batch.items.append({
                "file_id": tg_file.file_id,
                "file_unique_id": file_unique_id,
                "filename": filename,
                "content_type": content_type,
                "file_size": file_size,
                "message_id": message.message_id,
                "username": username,
                "tg_user_id": tg_user_id,
                "auth_token": upload_auth_token,
            })
            batch.updated_at = time.monotonic()
            if batch.first_message_id is None or message.message_id < batch.first_message_id:
                batch.first_message_id = message.message_id

            if batch.status_message_id is None:
                try:
                    status_msg = await message.reply_text("⏳ 正在处理相册图片，请稍候...")
                    batch.status_message_id = status_msg.message_id
                except Exception:
                    pass

            if batch.flush_task:
                batch.flush_task.cancel()
            batch.flush_task = asyncio.create_task(
                _flush_media_group(batch_key, context.bot, debounce_seconds=1.5)
            )
            return

        file_info = await asyncio.wait_for(context.bot.get_file(tg_file.file_id), timeout=_DOWNLOAD_TIMEOUT)
        file_bytes = await asyncio.wait_for(file_info.download_as_bytearray(), timeout=_DOWNLOAD_TIMEOUT)

        if is_group:
            result = record_existing_telegram_file(
                file_id=tg_file.file_id,
                file_unique_id=file_unique_id,
                file_path=getattr(file_info, 'file_path', '') or '',
                file_content=bytes(file_bytes),
                filename=filename,
                content_type=content_type,
                username=username,
                tg_user_id=tg_user_id,
                source='telegram_group',
                auth_token=upload_auth_token,
                is_group_upload=True,
                group_message_id=message.message_id,
                group_chat_id=chat.id,
            )
        else:
            result = process_upload(
                file_content=bytes(file_bytes),
                filename=filename,
                content_type=content_type,
                username=username,
                tg_user_id=tg_user_id,
                source='telegram_bot',
                auth_token=upload_auth_token,
                is_group_upload=False,
                group_message_id=None,
                upload_scene=None
            )

        if not reply_enabled:
            return

        if result:
            _inc_bot_stats(success=1)
            base_url = get_image_domain(None, scene='guest')
            permanent_url = f"{base_url}/image/{result['encrypted_id']}"

            link_formats = str(get_system_setting('bot_reply_link_formats') or 'url')
            text, parse_mode = _build_reply_text(result, permanent_url, filename, get_system_setting)

            # 私聊场景添加 inline 按钮
            reply_markup = None
            if not is_group and str(get_system_setting('bot_inline_buttons_enabled') or '1') == '1':
                from .commands import build_upload_success_keyboard
                reply_markup = build_upload_success_keyboard(
                    permanent_url, result['encrypted_id'], link_formats
                )

            reply_msg_id = None
            if status_msg:
                await status_msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
                reply_msg_id = status_msg.message_id
            else:
                sent = await message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
                reply_msg_id = sent.message_id

            # 群组延迟删除回复
            if is_group and delete_delay > 0 and reply_msg_id:
                async def delayed_delete():
                    try:
                        await asyncio.sleep(delete_delay)
                        await context.bot.delete_message(chat_id=chat.id, message_id=reply_msg_id)
                    except Exception as e:
                        logger.debug(f"删除回复消息失败: {e}")
                asyncio.create_task(delayed_delete())
        else:
            _inc_bot_stats(failed=1)
            if status_msg:
                await status_msg.edit_text("❌ 存储后端处理失败，请稍后重试")
    except asyncio.TimeoutError:
        _inc_bot_stats(failed=1)
        logger.error("文件下载超时")
        if status_msg:
            try:
                await status_msg.edit_text("❌ 文件下载超时，请检查网络后重试")
            except Exception:
                pass
    except Exception as e:
        _inc_bot_stats(failed=1)
        err_type = type(e).__name__
        logger.error(f"Error processing photo: {err_type}: {e}")

        if "Forbidden" in str(e) or "权限" in str(e):
            err_msg = "❌ 权限不足，请检查 Bot 权限设置"
        elif "NetworkError" in err_type or "TimedOut" in err_type:
            err_msg = "❌ 网络错误，请稍后重试"
        elif "BadRequest" in err_type:
            err_msg = "❌ 文件无法处理（可能过大或格式不支持）"
        else:
            err_msg = f"❌ 处理失败（{err_type}），请重试"

        if status_msg:
            try:
                await status_msg.edit_text(err_msg)
            except Exception:
                pass


async def handle_verify_text(update: Update, context):
    """处理文本消息 — web_verify 验证码登录"""
    from ..database import consume_web_verify_code, upsert_tg_user

    raw_text = (update.message.text or '').strip()
    # 兼容旧版 6 位数字验证码 + 新版 8 位字母数字验证码（排除易混淆字符）
    if not re.match(r'^(?:\d{6}|[A-HJ-NP-Z2-9]{8})$', raw_text, re.IGNORECASE):
        return  # 非验证码格式，静默忽略
    text = raw_text.upper()

    user = update.effective_user
    if not user:
        return

    # 确保用户已注册
    upsert_tg_user(user.id, user.username, user.first_name, user.last_name)

    result = consume_web_verify_code(text, user.id)
    if result:
        await update.message.reply_text("✅ 登录成功！请返回 Web 端")
    else:
        await update.message.reply_text("❌ 验证码无效或已过期")
