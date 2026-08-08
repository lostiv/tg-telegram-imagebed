#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot 命令模块

包含 /help, /id, /myuploads, /delete 命令处理，
callback_query 统一分发，以及上传成功 inline keyboard 构建。
"""
import math
import re
import time
from html import escape as html_escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ..config import logger
from ..utils import format_size


# ===================== 工具函数 =====================

def _sanitize_filename(caption: str, original_ext: str) -> str:
    """清理 caption 文本为安全文件名（保留原始扩展名）

    Args:
        caption: 用户发送的说明文字
        original_ext: 原始文件扩展名（含点号，如 '.jpg'）

    Returns:
        安全的文件名字符串
    """
    # 去除首尾空白
    name = caption.strip()
    # 移除路径分隔符和危险字符
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name)
    # 压缩连续下划线/空格
    name = re.sub(r'[_\s]+', '_', name).strip('_')
    # 限制长度（Telegram callback_data 64字节限制 + 文件系统兼容）
    if len(name.encode('utf-8')) > 100:
        while len(name.encode('utf-8')) > 100 and name:
            name = name[:-1]
        name = name.strip('_')
    # 空名称回退
    if not name:
        return None
    return f"{name}{original_ext}"


def build_upload_success_keyboard(
    permanent_url: str, encrypted_id: str,
    link_formats: str = 'url', active_fmt: str = 'url',
) -> InlineKeyboardMarkup:
    """构建上传成功后的 inline keyboard（私聊场景）

    Args:
        permanent_url: 图片永久直链
        encrypted_id: 加密文件ID（用于删除回调）
        link_formats: 启用的链接格式（逗号分隔：url,markdown,html,bbcode）
        active_fmt: 当前选中的格式（按钮显示 ✅ 前缀）
    """
    rows = []

    # 第一行：格式切换按钮（根据启用的格式动态生成）
    fmt_set = {f.strip().lower() for f in link_formats.split(',') if f.strip()}
    fmt_buttons = []
    all_fmts = [
        ('url', 'URL'),
        ('markdown', 'Markdown'),
        ('html', 'HTML'),
        ('bbcode', 'BBCode'),
    ]
    for fmt_key, label in all_fmts:
        if fmt_key in fmt_set:
            prefix = "✅ " if fmt_key == active_fmt else "📋 "
            fmt_buttons.append(
                InlineKeyboardButton(
                    f"{prefix}{label}",
                    callback_data=f"lfmt:{fmt_key}:{encrypted_id}",
                )
            )
    if fmt_buttons:
        rows.append(fmt_buttons)

    # 第二行：打开链接 + 删除
    rows.append([
        InlineKeyboardButton("🔗 打开链接", url=permanent_url),
        InlineKeyboardButton("🗑 删除", callback_data=f"qdel:{encrypted_id}"),
    ])

    return InlineKeyboardMarkup(rows)


# ===================== 命令处理器 =====================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令 — 显示所有可用命令"""
    text = (
        "📖 *可用命令列表*\n\n"
        "/start — 查看机器人状态和统计\n"
        "/help — 显示此帮助信息\n"
        "/id — 查看你的 Telegram ID 和聊天信息\n"
        "/myuploads — 查看个人上传历史\n"
        "/delete <ID> — 删除你上传的图片\n"
        "/login — 获取 Web 端登录链接\n"
        "/mytokens — 查看我的 Token\n"
        "/settoken — 设置默认上传 Token\n\n"
        "💡 *使用方法*\n"
        "直接发送图片即可获取永久直链\n"
        "发送图片时附带说明文字可自定义文件名"
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /id 命令 — 显示用户 ID 和聊天信息"""
    user = update.effective_user
    chat = update.effective_chat

    lines = ["🆔 <b>你的信息</b>\n"]
    if user:
        lines.append(f"👤 <b>用户 ID:</b> <code>{user.id}</code>")
        if user.username:
            lines.append(f"📛 <b>用户名:</b> @{html_escape(user.username)}")
        lines.append(f"📝 <b>全名:</b> {html_escape(user.full_name or '')}")
    if chat:
        lines.append(f"\n💬 <b>聊天 ID:</b> <code>{chat.id}</code>")
        lines.append(f"📋 <b>聊天类型:</b> {html_escape(str(chat.type))}")
        if chat.title:
            lines.append(f"📌 <b>聊天标题:</b> {html_escape(chat.title)}")

    lines.append("\n💡 将用户 ID 添加到 <code>group_admin_ids</code> 可获得群组管理权限")
    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


async def myuploads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /myuploads 命令 — 查看个人上传历史"""
    from ..database import get_system_setting

    if str(get_system_setting('bot_myuploads_enabled') or '1') != '1':
        await update.message.reply_text("❌ 上传历史查询功能已关闭")
        return

    user = update.effective_user
    if not user:
        await update.message.reply_text("❌ 无法获取用户信息")
        return
    username = user.username or user.full_name or str(user.id)
    await _show_myuploads(update.message, user.id, username, page=1)


async def _show_myuploads(
    message_or_query,
    tg_user_id: int,
    username: str,
    page: int = 1,
    edit: bool = False
):
    """展示上传历史（支持首次发送和翻页编辑）

    Args:
        message_or_query: Message 对象或 CallbackQuery 对象
        username: 用户名
        page: 页码
        edit: 是否编辑现有消息（翻页时为 True）
    """
    from ..database import get_user_uploads, get_system_setting

    try:
        per_page = max(1, min(50, int(get_system_setting('bot_myuploads_page_size') or '8')))
    except (TypeError, ValueError):
        per_page = 8
    files, total = get_user_uploads(username, tg_user_id=tg_user_id, limit=per_page, page=page)
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)

    if total == 0:
        text = "📭 你还没有上传过图片"
        if edit:
            await message_or_query.edit_message_text(text)
        else:
            await message_or_query.reply_text(text)
        return

    lines = [f"📋 <b>你的上传记录</b>（共 {total} 张，第 {page}/{total_pages} 页）\n"]
    for f in files:
        name = html_escape(str(f.get('original_filename') or f['encrypted_id'][:12]))
        size_str = format_size(f.get('file_size') or 0)
        eid = html_escape(str(f['encrypted_id'][:12]))
        lines.append(f"• <code>{eid}</code> | {name} | {size_str}")

    text = '\n'.join(lines)

    # 构建翻页按钮
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"myup:{page - 1}"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"myup:{page + 1}"))

    markup = InlineKeyboardMarkup([buttons]) if buttons else None

    if edit:
        await message_or_query.edit_message_text(text, parse_mode='HTML', reply_markup=markup)
    else:
        await message_or_query.reply_text(text, parse_mode='HTML', reply_markup=markup)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /delete <ID> 命令 — 删除自己上传的图片"""
    from ..database import get_file_info, get_system_setting

    if str(get_system_setting('bot_user_delete_enabled') or '1') != '1':
        await update.message.reply_text("❌ 自助删除功能已关闭")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ 请提供文件 ID\n\n用法: `/delete <ID>`\n"
            "💡 使用 /myuploads 查看你的文件 ID",
            parse_mode='Markdown'
        )
        return

    encrypted_id = context.args[0].strip()
    user = update.effective_user
    if not user:
        await update.message.reply_text("❌ 无法获取用户信息")
        return

    # 查询文件并验证所有权
    file_info = get_file_info(encrypted_id)
    if not file_info:
        await update.message.reply_text("❌ 文件不存在")
        return

    if not _is_file_owner(file_info, user):
        await update.message.reply_text("❌ 你没有权限删除此文件")
        return

    # 弹出确认按钮
    name = html_escape(str(file_info.get('original_filename') or encrypted_id[:12]))
    size_str = format_size(file_info.get('file_size') or 0)
    short_id = html_escape(str(encrypted_id[:16]))
    text = (
        f"⚠️ <b>确认删除？</b>\n\n"
        f"📄 <b>文件:</b> {name}\n"
        f"🆔 <b>ID:</b> <code>{short_id}</code>\n"
        f"📊 <b>大小:</b> {size_str}\n\n"
        f"此操作不可撤销！"
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 确认删除", callback_data=f"cdel:{encrypted_id}:y"),
        InlineKeyboardButton("❌ 取消", callback_data=f"cdel:{encrypted_id}:n"),
    ]])
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=markup)


# ===================== Callback 统一分发 =====================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """统一 callback_query 入口，按前缀分发"""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    data = query.data
    if data.startswith("myup:"):
        await _handle_myuploads_page(query)
    elif data.startswith("cdel:"):
        await _handle_confirm_delete(query)
    elif data.startswith("qdel:"):
        await _handle_quick_delete(query)
    elif data.startswith("lfmt:"):
        await _handle_link_format_callback(query)
    elif data.startswith("stk:"):
        await _handle_settoken_callback(query)
    else:
        logger.warning(f"未知 callback_data: {data}")


async def _handle_myuploads_page(query):
    """处理上传历史翻页"""
    from ..database import get_system_setting

    if str(get_system_setting('bot_myuploads_enabled') or '1') != '1':
        await query.edit_message_text("❌ 上传历史查询功能已关闭")
        return

    try:
        page = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        return

    user = query.from_user
    if not user:
        return
    username = user.username or user.full_name or str(user.id)
    await _show_myuploads(query, user.id, username, page=page, edit=True)


async def _handle_confirm_delete(query):
    """处理 /delete 确认/取消回调"""
    from ..database import get_file_info, get_system_setting
    from ..services.file_service import delete_file_with_storage

    parts = query.data.split(":")
    if len(parts) != 3:
        return

    encrypted_id, action = parts[1], parts[2]

    if action == 'n':
        await query.edit_message_text("❎ 已取消删除")
        return

    if str(get_system_setting('bot_user_delete_enabled') or '1') != '1':
        await query.edit_message_text("❌ 自助删除功能已关闭")
        return

    # 验证所有权
    user = query.from_user
    if not user:
        return

    file_info = get_file_info(encrypted_id)
    if not file_info:
        await query.edit_message_text("❌ 文件不存在或已被删除")
        return

    if not _is_file_owner(file_info, user):
        await query.edit_message_text("❌ 你没有权限删除此文件")
        return

    if delete_file_with_storage(encrypted_id):
        await query.edit_message_text("✅ 文件已成功删除")
    else:
        await query.edit_message_text("❌ 删除失败，请稍后重试")


async def _handle_quick_delete(query):
    """处理上传成功后的快速删除按钮"""
    from ..database import get_file_info, get_system_setting
    from ..services.file_service import delete_file_with_storage

    if str(get_system_setting('bot_user_delete_enabled') or '1') != '1':
        await query.edit_message_text("❌ 自助删除功能已关闭")
        return

    encrypted_id = query.data.split(":", 1)[1] if ":" in query.data else ""
    if not encrypted_id:
        return

    user = query.from_user
    if not user:
        return

    file_info = get_file_info(encrypted_id)
    if not file_info:
        await query.edit_message_text("❌ 文件不存在或已被删除")
        return

    if not _is_file_owner(file_info, user):
        await query.edit_message_text("❌ 你没有权限删除此文件")
        return

    if delete_file_with_storage(encrypted_id):
        await query.edit_message_text("✅ 文件已删除")
    else:
        await query.edit_message_text("❌ 删除失败，请稍后重试")


async def _handle_link_format_callback(query):
    """处理链接格式按钮点击（lfmt:<format>:<encrypted_id>）

    点击后将消息中的代码块内容替换为对应格式，方便用户长按复制。
    使用 HTML parse_mode 避免 Markdown 特殊字符解析问题。
    """
    from html import escape as html_escape
    from ..database import get_file_info, get_system_setting
    from ..utils import get_image_domain, format_size

    try:
        parts = query.data.split(":", 2)
        if len(parts) != 3:
            logger.warning(f"lfmt 回调数据格式错误: {query.data}")
            return

        fmt, encrypted_id = parts[1], parts[2]

        file_info = get_file_info(encrypted_id)
        if not file_info:
            logger.warning(f"lfmt 回调: 文件不存在 encrypted_id={encrypted_id}")
            return

        base_url = get_image_domain(None)
        url = f"{base_url}/image/{encrypted_id}"

        # 各格式的代码块内容
        format_map = {
            'url':      url,
            'markdown': f"![image]({url})",
            'html':     f'<img src="{url}" />',
            'bbcode':   f"[img]{url}[/img]",
        }
        code_content = format_map.get(fmt)
        if code_content is None:
            logger.warning(f"lfmt 回调: 未知格式 fmt={fmt}")
            return

        # 重建完整消息（使用 HTML parse_mode）
        show_size = str(get_system_setting('bot_reply_show_size') or '1') == '1'
        show_filename = str(get_system_setting('bot_reply_show_filename') or '0') == '1'
        link_formats = str(get_system_setting('bot_reply_link_formats') or 'url')

        lines = [
            "✅ <b>上传成功！</b>\n",
            f"🔗 <b>永久直链:</b>\n<code>{html_escape(code_content)}</code>\n",
        ]
        if show_filename:
            fname = html_escape(file_info.get('original_filename') or encrypted_id[:12])
            lines.append(f"📄 <b>文件名:</b> {fname}")
        if show_size:
            lines.append(f"📊 <b>文件大小:</b> {format_size(file_info.get('file_size') or 0)}")
        lines.append("💡 链接永久有效")
        text = '\n'.join(lines)

        # 重建 keyboard（当前格式按钮高亮，其余可切换）
        keyboard = build_upload_success_keyboard(url, encrypted_id, link_formats, active_fmt=fmt)

        await query.edit_message_text(
            text,
            parse_mode='HTML',
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.warning(f"处理链接格式回调异常: {type(e).__name__}: {e}")


async def _handle_settoken_callback(query):
    """处理 /settoken 选择回调"""
    from ..database import set_default_upload_token

    user = query.from_user
    if not user:
        return

    try:
        idx = int(query.data[len('stk:'):])
    except (ValueError, IndexError):
        return

    # 从缓存中取出该用户的 token 列表
    token_list = _pop_pending_tokens(user.id)
    if not token_list or idx < 0 or idx >= len(token_list):
        await query.edit_message_text("❌ 选择已过期，请重新使用 /settoken")
        return

    token = token_list[idx]
    if set_default_upload_token(user.id, token):
        masked = html_escape(_mask_token(token))
        await query.edit_message_text(
            f"✅ 默认上传 Token 已更新\n🔑 <code>{masked}</code>",
            parse_mode='HTML'
        )
    else:
        await query.edit_message_text("❌ 设置失败，请检查 Token 是否有效")


# ===================== TG 认证命令 =====================

# /settoken 待选缓存：{tg_user_id: {"tokens": [...], "expires_at": float}}
_settoken_pending: dict[int, dict] = {}


def _is_file_owner(file_info: dict, user) -> bool:
    """校验文件所有权：优先 tg_user_id，兼容 username 历史数据"""
    if not user or not file_info:
        return False
    file_tg_user_id = file_info.get('tg_user_id')
    if file_tg_user_id is not None:
        try:
            return int(file_tg_user_id) == int(user.id)
        except (TypeError, ValueError):
            return False
    username = user.username or user.full_name or str(user.id)
    return file_info.get('username') == username


def _get_settoken_ttl_seconds() -> int:
    """读取 /settoken 回调有效期"""
    from ..database import get_system_setting_int
    return get_system_setting_int('bot_settoken_ttl_seconds', 600, minimum=30, maximum=3600)


def _prune_pending_tokens(now_ts: float = None) -> None:
    """清理过期待选缓存，防止内存累积"""
    now_ts = now_ts if now_ts is not None else time.time()
    expired_ids = [
        uid for uid, payload in _settoken_pending.items()
        if float(payload.get('expires_at') or 0) <= now_ts
    ]
    for uid in expired_ids:
        _settoken_pending.pop(uid, None)


def _set_pending_tokens(tg_user_id: int, token_list: list[str]) -> None:
    """写入待选缓存（带 TTL）"""
    now_ts = time.time()
    _prune_pending_tokens(now_ts)
    ttl = _get_settoken_ttl_seconds()
    _settoken_pending[tg_user_id] = {
        "tokens": list(token_list),
        "expires_at": now_ts + ttl,
    }


def _pop_pending_tokens(tg_user_id: int) -> list[str] | None:
    """读取并删除待选缓存（过期返回 None）"""
    payload = _settoken_pending.pop(tg_user_id, None)
    if not payload:
        return None
    if float(payload.get('expires_at') or 0) <= time.time():
        return None
    tokens = payload.get("tokens")
    return list(tokens) if isinstance(tokens, list) else None


def _mask_token(token: str) -> str:
    """将 Token 脱敏显示（前8后4）"""
    if len(token) > 12:
        return f"{token[:8]}…{token[-4:]}"
    return token


async def settoken_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /settoken 命令 — 选择默认上传 Token"""
    from ..database import get_active_user_tokens

    user = update.effective_user
    if not user:
        await update.message.reply_text("❌ 无法获取用户信息")
        return

    tokens = get_active_user_tokens(user.id)
    if not tokens:
        await update.message.reply_text("❌ 你还没有绑定任何 Token\n\n💡 请先通过 Web 端登录并生成 Token")
        return

    # 始终弹出选择列表
    _set_pending_tokens(user.id, [t['token'] for t in tokens])
    buttons = []
    for i, t in enumerate(tokens):
        masked = _mask_token(t['token'])
        label = f"{'✅ ' if t['is_default_upload'] else ''}{masked}"
        if t.get('description'):
            label += f" ({t['description']})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"stk:{i}")])

    markup = InlineKeyboardMarkup(buttons)
    header = f"🔑 选择默认上传 Token（共 {len(tokens)} 个）："
    await update.message.reply_text(header, reply_markup=markup)


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /login 命令 — 生成 Web 端一次性登录链接"""
    from ..database import get_system_setting, upsert_tg_user, create_login_code
    from ..utils import get_domain

    if str(get_system_setting('tg_auth_enabled') or '0') != '1':
        await update.message.reply_text("❌ TG 认证功能未启用")
        return

    user = update.effective_user
    if not user:
        await update.message.reply_text("❌ 无法获取用户信息")
        return

    # 记录/更新用户信息
    upsert_tg_user(
        tg_user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )

    # 生成一次性登录链接
    code = create_login_code(code_type='login_link', tg_user_id=user.id)
    if not code:
        await update.message.reply_text("❌ 生成登录链接失败，请稍后重试")
        return

    base_url = get_domain(None)
    login_url = f"{base_url}/tg-login?code={code}"

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 点击登录 Web 端", url=login_url)
    ]])

    await update.message.reply_text(
        "🔐 *Web 端登录*\n\n"
        "点击下方按钮登录图床 Web 端：\n\n"
        "⏰ 链接有效期 5 分钟，仅可使用一次",
        parse_mode='Markdown',
        reply_markup=markup
    )


async def mytokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /mytokens 命令 — 查看绑定的 Token（增强版）"""
    from datetime import datetime
    from ..database import get_system_setting, get_user_tokens
    from ..utils import get_domain

    if str(get_system_setting('tg_auth_enabled') or '0') != '1':
        await update.message.reply_text("❌ TG 认证功能未启用")
        return

    user = update.effective_user
    if not user:
        await update.message.reply_text("❌ 无法获取用户信息")
        return

    tokens = get_user_tokens(user.id)
    if not tokens:
        await update.message.reply_text("📭 你还没有绑定任何 Token\n\n💡 通过 Web 端登录后生成的 Token 会自动绑定")
        return

    lines = [f"🔑 <b>你的 Token</b>（共 {len(tokens)} 个）\n"]
    for t in tokens:
        token_str = t['token']
        masked = html_escape(f"{token_str[:8]}…{token_str[-4:]}" if len(token_str) > 12 else token_str)
        status = "✅" if t['is_active'] else "🚫"
        usage = f"{t['upload_count']}/{t['upload_limit']}"
        desc = html_escape(str(t.get('description') or ''))
        desc_str = f" | {desc}" if desc else ''

        # 过期状态
        expire_str = ""
        if t.get('expires_at'):
            try:
                exp_dt = datetime.fromisoformat(str(t['expires_at']).replace('Z', '+00:00'))
                if exp_dt.tzinfo is not None:
                    exp_dt = exp_dt.astimezone().replace(tzinfo=None)
                now = datetime.now()
                if now > exp_dt:
                    expire_str = " ⏰已过期"
                else:
                    delta = exp_dt - now
                    days_left = delta.days
                    if days_left > 30:
                        expire_str = f" | 剩余{days_left}天"
                    elif days_left > 0:
                        expire_str = f" | ⚠️剩余{days_left}天"
                    else:
                        hours_left = int(delta.total_seconds() / 3600)
                        expire_str = f" | ⚠️剩余{hours_left}小时"
            except (ValueError, TypeError):
                pass

        # 最后使用时间
        last_used_str = ""
        if t.get('last_used'):
            try:
                lu_dt = datetime.fromisoformat(str(t['last_used']).replace('Z', '+00:00'))
                if lu_dt.tzinfo is not None:
                    lu_dt = lu_dt.astimezone().replace(tzinfo=None)
                last_used_str = f"\n  📅 最后使用: {lu_dt.strftime('%m-%d %H:%M')}"
            except (ValueError, TypeError):
                pass

        lines.append(f"• <code>{masked}</code> {status} {usage}{desc_str}{expire_str}{last_used_str}")

    # 构建 inline 按钮：跳转 Web 端
    base_url = get_domain(None)
    buttons = []
    if base_url:
        buttons.append([InlineKeyboardButton("🌐 在 Web 端管理", url=f"{base_url}/album")])
    markup = InlineKeyboardMarkup(buttons) if buttons else None

    await update.message.reply_text('\n'.join(lines), parse_mode='HTML', reply_markup=markup)
