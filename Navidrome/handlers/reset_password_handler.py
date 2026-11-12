from telegram import Update
from telegram.ext import (CallbackContext, CallbackQueryHandler, ConversationHandler,
                             CommandHandler, MessageHandler, filters)
from database import users_collection
from services.navidrome_client import navidrome_service
from handlers.permissions import private_only
from handlers.del_user_handler import _resolve_username
from log import logger

# 会话等待状态
AWAITING_PW = 9101
MIN_LEN = 6


def _pw_check(pw):
    """校验密码强度，返回 (是否通过, 原因)。"""
    pw = str(pw or "")
    if len(pw) < MIN_LEN:
        return False, f"密码至少 {MIN_LEN} 位"
    if " " in pw:
        return False, "密码不能包含空格"
    if pw.isdigit():
        return False, "密码不能是纯数字"
    if pw.isalpha():
        return False, "密码不能是纯字母"
    return True, ""


async def _do_reset(chat, username, new_password, user_name=None):
    """执行重置，返回提示文本。"""
    user_doc = users_collection.find_one({"username": username}) or {}
    user_id = user_doc.get("user_id")
    name = user_name or user_doc.get("name") or username
    if not user_id:
        whole = await navidrome_service.list_users() if hasattr(navidrome_service, "list_users") else None
        for c in navidrome_service.clients:
            try:
                u = await c.find_user(username)
                if u and u.get("id"):
                    user_id = u["id"]
                    name = u.get("name") or name
                    break
            except Exception:  # noqa: BLE001
                continue
    if not user_id:
        return f"❌ {username}：在服务器上找不到这个账号的 ID，无法重置"

    result = await navidrome_service.reset_password(user_id, name, username, new_password)
    if result.code != 200:
        return f"❌ {username} 重置失败：{result.message or result.data}"

    failed = (result.data or {}).get("failed") or []
    lines = [f"✅ {username} 的新密码：`{new_password}`"]
    if failed:
        lines.append(f"⚠️ {'、'.join(failed)} 暂时连不上，稍后会自动同步新密码")
    lines.append("请自行保存，本条消息可删除。")
    return "\n".join(lines)


@private_only
async def reset_password(update: Update, context: CallbackContext):
    """点击「重置密码」按钮，进入自定义密码会话。"""
    query = update.callback_query
    await query.answer(cache_time=5)
    uid = query.from_user.id
    is_admin = users_collection.find_one({"telegram_id": uid, "is_admin": True}) is not None
    if not is_admin:
        try:
            from config import ADMIN_ID
            is_admin = uid in ADMIN_ID if isinstance(ADMIN_ID, (list, tuple)) else uid == ADMIN_ID
        except Exception:  # noqa: BLE001
            is_admin = False

    context.user_data["admin_reset"] = is_admin
    if is_admin:
        tip = "请输入：账号 新密码\n例如：yourname Abc123456\n\n发 /cancel 取消"
    else:
        tip = "请输入你的新密码（至少 6 位，不能纯数字/纯字母/含空格）\n\n发 /cancel 取消"

    await query.message.reply_text(tip)
    return AWAITING_PW


async def _reset_pw_cmd(update: Update, context: CallbackContext):
    """/reset_pw 账号 新密码  —— 一行搞定。"""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "用法：/reset_pw 账号 新密码\n例如：/reset_pw yourname Abc123456")
        return

    token = args[0]
    new_password = " ".join(args[1:]).strip()

    ok, why = _pw_check(new_password)
    if not ok:
        await update.message.reply_text(f"❌ {why}")
        return

    real_name, how = await _resolve_username(token)
    if not real_name:
        await update.message.reply_text(f"❌ {token}：{how}")
        return

    text = await _do_reset(update.effective_chat, real_name, new_password)
    if how != "db":
        text = f"（匹配到 {real_name}）\n" + text
    await update.message.reply_text(text, parse_mode="Markdown")
    logger.info(f"重置密码：{token} -> {real_name}")


async def _receive_password(update: Update, context: CallbackContext):
    """会话中接收管理员/用户输入的密码。"""
    text = (update.message.text or "").strip()
    is_admin = context.user_data.get("admin_reset", False)

    if is_admin:
        parts = text.split(None, 1)
        if len(parts) < 2:
            await update.message.reply_text("格式：账号 新密码\n例如：yourname Abc123456\n\n发 /cancel 取消")
            return AWAITING_PW
        token, new_password = parts[0], parts[1].strip()
        ok, why = _pw_check(new_password)
        if not ok:
            await update.message.reply_text(f"❌ {why}\n请重新输入（发 /cancel 取消）")
            return AWAITING_PW
        real_name, how = await _resolve_username(token)
        if not real_name:
            await update.message.reply_text(f"❌ {token}：{how}\n请重新输入（发 /cancel 取消）")
            return AWAITING_PW
        reply = await _do_reset(update.effective_chat, real_name, new_password)
        if how != "db":
            reply = f"（匹配到 {real_name}）\n" + reply
        await update.message.reply_text(reply, parse_mode="Markdown")
        context.user_data.pop("admin_reset", None)
        return ConversationHandler.END

    # 普通用户改自己的密码
    ok, why = _pw_check(text)
    if not ok:
        await update.message.reply_text(f"❌ {why}\n请重新输入（发 /cancel 取消）")
        return AWAITING_PW

    user_doc = users_collection.find_one({"telegram_id": update.effective_user.id})
    if not user_doc or not user_doc.get("username"):
        await update.message.reply_text("❌ 你还没有绑定账号")
        return ConversationHandler.END

    reply = await _do_reset(update.effective_chat, user_doc["username"], text,
                            user_doc.get("name"))
    await update.message.reply_text(reply, parse_mode="Markdown")
    return ConversationHandler.END


async def _cancel(update: Update, context: CallbackContext):
    context.user_data.pop("admin_reset", None)
    await update.message.reply_text("已取消")
    return ConversationHandler.END


def build_reset_conv():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(reset_password, pattern="^reset_password$")],
        states={AWAITING_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_password)]},
        fallbacks=[CommandHandler("cancel", _cancel)],
        allow_reentry=True,
    )
