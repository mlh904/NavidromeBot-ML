"""绑定 / 解绑已有音乐账号。

场景：Bot 的数据备份不全，有些用户已在服务器上注册，但 Bot 里没有记录。
这些用户可以用 /bind 输入账号 + 密码，验证通过后把 Telegram ID 关联上；
用 /unbind 解除关联（服务器上的账号保留，可以再绑回来）。
"""
from telegram import Update
from telegram.ext import (CallbackContext, CommandHandler, ConversationHandler,
                          MessageHandler, filters)

from database import users_collection
from services.navidrome_client import navidrome_service
from handlers.permissions import private_only
from util import get_now_utc
from log import logger

# 会话状态（与其它 handler 的状态号区分开）
AWAITING_BIND_USER = 9201
AWAITING_BIND_PW = 9202


def _find_doc_by_tg(tg_id):
    return users_collection.find_one({"telegram_id": tg_id})


async def _locate_account(username):
    """在各台服务器上查找账号，返回 (user_dict, ok_client, 命中服务器列表)。"""
    found, hit_servers, first_client = None, [], None
    for c in navidrome_service.clients:
        try:
            u = await c.find_user(username)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bind] {c.name} 查询失败：{type(e).__name__}")
            continue
        if u:
            hit_servers.append(c.name)
            if found is None:
                found = u
                first_client = c
    return found, first_client, hit_servers


async def _verify_password(client, username, password):
    """用某台服务器验证账号密码，返回 (成功?, 错误信息)。"""
    try:
        data = await client.login_as(username, password)
        return (True, None) if data else (False, "密码不对")
    except Exception as e:  # noqa: BLE001
        return False, f"无法连接 {client.name}：{type(e).__name__}"


def _bind_cmd_help():
    return (
        "🔗 绑定已有账号\n\n"
        "如果你已经在服务器上注册过，但机器人里没有你的记录，用这个命令绑定：\n\n"
        "/bind 账号名\n\n"
        "然后按提示输入你的密码。\n"
        "发 /cancel 取消"
    )


@private_only
async def bind_start(update: Update, context: CallbackContext):
    """/bind —— 开始绑定流程。"""
    tg_id = update.effective_user.id

    # 已经绑过了就提示，不重复绑
    doc = _find_doc_by_tg(tg_id)
    if doc and doc.get("user_id"):
        await update.message.reply_text(
            f"你已经绑定了账号：`{doc.get('username')}`\n\n"
            "如果想换一个账号，先发 /unbind 解绑。",
            parse_mode="Markdown")
        return ConversationHandler.END

    args = context.args or []
    if args:
        # 直接 /bind 账号名 一步到位
        context.user_data["bind_username"] = args[0].strip()
        await update.message.reply_text(
            f"账号：`{context.user_data['bind_username']}`\n\n"
            "请输入这个账号的密码。\n发 /cancel 取消",
            parse_mode="Markdown")
        return AWAITING_BIND_PW

    await update.message.reply_text(_bind_cmd_help())
    return AWAITING_BIND_USER


async def bind_receive_username(update: Update, context: CallbackContext):
    """会话中接收账号名。"""
    username = (update.message.text or "").strip()
    if not username or len(username) > 64:
        await update.message.reply_text("账号名不对，请重新输入\n发 /cancel 取消")
        return AWAITING_BIND_USER
    context.user_data["bind_username"] = username
    await update.message.reply_text(
        f"账号：`{username}`\n\n请输入这个账号的密码。\n发 /cancel 取消",
        parse_mode="Markdown")
    return AWAITING_BIND_PW


async def bind_receive_password(update: Update, context: CallbackContext):
    """会话中接收密码，验证通过后写入绑定。"""
    password = (update.message.text or "").strip()
    username = context.user_data.get("bind_username", "")
    tg_user = update.effective_user
    if not username:
        await update.message.reply_text("会话已过期，请重新发 /bind")
        return ConversationHandler.END

    if not password:
        await update.message.reply_text("密码不能为空，请重新输入\n发 /cancel 取消")
        return AWAITING_BIND_PW

    # 1) 服务器上找账号
    user, client, hit_servers = await _locate_account(username)
    if not user:
        await update.message.reply_text(
            f"❌ 在所有服务器上都找不到账号 `{username}`\n\n"
            "请检查账号名是否正确。如果确实没注册过，请找管理员开通。\n"
            "发 /cancel 取消", parse_mode="Markdown")
        return AWAITING_BIND_PW

    # 2) 验证密码
    ok, why = await _verify_password(client, user.get("userName") or username, password)
    if not ok:
        logger.info(f"[bind] {username} 密码验证失败：{why}")
        await update.message.reply_text(
            f"❌ {why}\n\n请重新输入密码。发 /cancel 取消")
        return AWAITING_BIND_PW

    # 3) 检查这个账号是不是已经被别的 Telegram 绑了
    other = users_collection.find_one(
        {"username": {"$regex": f"^{username}$", "$options": "i"},
         "telegram_id": {"$ne": tg_user.id}})
    if other:
        await update.message.reply_text(
            "❌ 这个账号已经绑定了另一个 Telegram 账号。\n\n"
            "如果是你自己的号被误绑，请联系管理员处理。")
        context.user_data.pop("bind_username", None)
        return ConversationHandler.END

    # 4) 写入绑定
    now = get_now_utc()
    payload = {
        "telegram_id": tg_user.id,
        "username": user.get("userName") or username,
        "name": user.get("name") or username,
        "user_id": user.get("id"),
        "updated_at": now,
    }
    doc = _find_doc_by_tg(tg_user.id)
    if doc:
        users_collection.update_one({"_id": doc["_id"]}, {"$set": payload})
    else:
        payload.update({"created_at": now, "last_check_in": None, "bind_via": "self"})
        users_collection.insert_one(payload)

    context.user_data.pop("bind_username", None)
    logger.info(f"[bind] {tg_user.id} 绑定 {payload['username']} 成功")

    lines = [
        f"✅ 绑定成功",
        f"👥 账号：`{payload['username']}`",
        f"🆔 用户ID：`{payload['user_id']}`",
    ]
    if len(hit_servers) > 1:
        lines.append(f"🌐 该账号存在于：{'、'.join(hit_servers)}")
    lines.append("\n现在可以直接用 /start 打开主面板。")
    lines.append("⚠️ 你的密码只用于本次验证，机器人没有保存它。")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return ConversationHandler.END


@private_only
async def unbind(update: Update, context: CallbackContext):
    """/unbind —— 解除 Telegram 绑定，服务器账号保留。"""
    tg_id = update.effective_user.id
    doc = _find_doc_by_tg(tg_id)
    if not doc or not doc.get("user_id"):
        await update.message.reply_text("你现在没有绑定任何账号，不用解绑。")
        return

    username = doc.get("username") or "（未知）"

    # 只清掉关联字段，保留一条空壳记录不影响使用
    users_collection.delete_one({"_id": doc["_id"]})
    logger.info(f"[unbind] {tg_id} 解绑 {username}")

    await update.message.reply_text(
        f"✅ 已解绑账号 `{username}`\n\n"
        "服务器上的音乐账号仍然保留，你还可以正常登录网站听歌。\n"
        "以后想重新绑定，发 /bind 即可。",
        parse_mode="Markdown")


@private_only
async def bind_status(update: Update, context: CallbackContext):
    """/mybind —— 查看当前绑定情况。"""
    doc = _find_doc_by_tg(update.effective_user.id)
    if doc and doc.get("user_id"):
        await update.message.reply_text(
            f"👥 已绑定账号：`{doc.get('username')}`\n"
            f"🆔 用户ID：`{doc.get('user_id')}`",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "你还没有绑定账号。\n\n发 /bind 可以绑定一个已有账号。")


async def bind_cancel(update: Update, context: CallbackContext):
    context.user_data.pop("bind_username", None)
    await update.message.reply_text("已取消")
    return ConversationHandler.END


def build_bind_conv():
    return ConversationHandler(
        entry_points=[CommandHandler("bind", bind_start)],
        states={
            AWAITING_BIND_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, bind_receive_username)],
            AWAITING_BIND_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, bind_receive_password)],
        },
        fallbacks=[CommandHandler("cancel", bind_cancel)],
        allow_reentry=True,
    )
