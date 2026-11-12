import asyncio

from telegram.ext import CallbackContext
from telegram import Update
from log import logger
from database import users_collection, whitelist_collection
from config import ALLOWED_GROUP_IDS
from handlers.permissions import admin_only, is_user_in_allowed_group_id
from util import delete_messages
from services.navidrome_client import navidrome_service

# 删除用户的函数


_PREFIXES = ("账号：", "账号:", "账户：", "账户:", "用户名：", "用户名:", "用户：", "用户:")


def _norm(name):
    """去掉「账号：」之类的前缀并小写，便于模糊比对。"""
    s = str(name or "").strip().lower()
    for pfx in _PREFIXES:
        if s.startswith(pfx):
            s = s[len(pfx):].strip()
    return s


async def _resolve_username(token):
    """把用户输入解析成真实的 Navidrome 用户名。

    顺序：数据库精确 -> 各服务器精确 -> 模糊包含匹配。
    返回 (真实用户名 或 None, 说明)
    """
    token = str(token or "").strip()
    if not token:
        return None, "账号名不能为空"
    want = _norm(token)

    # 1) 数据库精确
    for doc in users_collection.find({}):
        nm = doc.get("username")
        if nm and str(nm).lower() == token.lower():
            return nm, "db"

    # 2) 各服务器精确 + 3) 模糊
    hits = set()
    for c in navidrome_service.clients:
        try:
            for u in await c.list_users():
                nm = u.get("userName") or ""
                if str(nm).lower() == token.lower():
                    return nm, "server"
                if _norm(nm) == want or want in _norm(nm):
                    hits.add(nm)
        except Exception:  # noqa: BLE001
            continue

    if len(hits) == 1:
        return hits.pop(), "fuzzy"
    if len(hits) > 1:
        return None, "匹配到多个账号：" + "、".join(sorted(hits)) + "  请写得更完整一些"
    return None, f"没有找到账号 {token}"


async def _find_user_everywhere(username):
    """逐台查用户是否存在，返回仍存在该账号的服务器名列表。"""
    remain = []
    for c in navidrome_service.clients:
        try:
            if await c.find_user(username):
                remain.append(c.name)
        except Exception:  # noqa: BLE001
            remain.append(f"{c.name}(连接失败)")
    return remain


async def delete_user_by_telegram_id(telegram_id, context: CallbackContext):
    user_data = users_collection.find_one({"telegram_id": telegram_id})
    mention = None
    for chat_id in ALLOWED_GROUP_IDS:
        try:
            chat_member = await context.bot.get_chat_member(chat_id, telegram_id)
            mention = chat_member.user.mention_html()
        except Exception as e:
            logger.warning(f"获取用户 {telegram_id} 信息失败：{e}")
        break
    if mention is None:
        mention = f'<a href="tg://user?id={telegram_id}">这虎揍</a>'
    if not user_data:
        logger.debug(f"用户 {telegram_id} 未找到。")
        return 400, mention, "没找到这虎揍！"

    logger.debug(f"user_data: {user_data}")  # 添加调试日志
    user_id = user_data.get("user_id")  # 使用 .get() 方法避免 KeyError
    if not user_id:
        return 400, mention, "Navidrome中没有此虎揍！"

    result = await navidrome_service.delete_user(user_id, user_data.get("username"))

    if result.code == 200:
        # 从数据库中删除用户记录
        users_collection.delete_one({"telegram_id": telegram_id})
        whitelist_collection.delete_one({"telegram_id": telegram_id})
        failed = (result.data or {}).get("failed") or []
        tip = f"（{'、'.join(failed)} 暂时连不上，稍后自动补删）" if failed else ""
        return 200, mention, f"这虎揍的账号已删除{tip}"
    else:
        return 400, mention, f"删除用户失败：{result.data}"

# 处理删除用户命令的函数


@admin_only
async def del_user(update: Update, context: CallbackContext):
    # 获取回复的消息
    replied_message = update.message.reply_to_message

    if replied_message:
        # 如果是回复消息，则删除被回复的用户
        telegram_id = replied_message.from_user.id
    else:
        if not context.args:
            reply_message = await update.message.reply_text(
                "使用方法：\n/del_user 账号名   （支持模糊，例如 yilizhigui）\n"
                "/del_user <telegram_id>\n或回复某人的消息使用 /del_user")
            context.job_queue.run_once(delete_messages, 8, data={
                'chat_id': update.message.chat.id,
                'user_message_id': update.message.message_id,
                'bot_message_id': reply_message.message_id
            })
            return

        arg = context.args[0].strip()

        # 纯数字 -> 当作 telegram_id
        if arg.isdigit() and len(arg) >= 5:
            telegram_id = int(arg)
        else:
            # 当作账号名，走模糊解析
            real_name, how = await _resolve_username(arg)
            if not real_name:
                msg = await update.message.reply_text(f"❌ {arg}：{how}")
                context.job_queue.run_once(delete_messages, 8, data={
                    'chat_id': update.message.chat.id,
                    'user_message_id': update.message.message_id,
                    'bot_message_id': msg.message_id
                })
                return

            result = await navidrome_service.delete_user(username=real_name)
            note = "" if how == "db" else f"（模糊匹配到 {real_name}）"
            if result.code != 200:
                msg = await update.message.reply_text(f"❌ 删除 {real_name} 失败：{result.data}")
                context.job_queue.run_once(delete_messages, 8, data={
                    'chat_id': update.message.chat.id,
                    'user_message_id': update.message.message_id,
                    'bot_message_id': msg.message_id
                })
                return

            # 删完逐台复查
            remain = await _find_user_everywhere(real_name)
            # 清理数据库记录
            users_collection.delete_many({"username": real_name})
            doc = users_collection.find_one({"username": real_name})
            if not doc:
                pass

            if remain:
                text = f"⚠️ {real_name}{note} 未删净，仍存在：{'、'.join(remain)}"
            else:
                text = f"✅ {real_name}{note} 已从所有服务器删除"

            msg = await update.message.reply_text(text)
            context.job_queue.run_once(delete_messages, 10, data={
                'chat_id': update.message.chat.id,
                'user_message_id': update.message.message_id,
                'bot_message_id': msg.message_id
            })
            logger.info(f"删除账号：{arg} -> {real_name} 结果：{text}")
            return

    code, mention, result = await delete_user_by_telegram_id(telegram_id, context)
    if code == 200:
        reply_message = await update.message.reply_text(
            f"{mention} {result}", parse_mode='HTML')
        context.job_queue.run_once(delete_messages, 5, data={
            'chat_id': update.message.chat.id,
            'user_message_id': update.message.message_id,
            'bot_message_id': reply_message.message_id
        })
    else:
        reply_message = await update.message.reply_text(
            f"{mention} {result}", parse_mode='HTML')
        context.job_queue.run_once(delete_messages, 5, data={
            'chat_id': update.message.chat.id,
            'user_message_id': update.message.message_id,
            'bot_message_id': reply_message.message_id
        })
    logger.info(f"删除用户：{telegram_id} 结果： {result}")

# 处理用户退出群组事件的函数


async def handle_left_chat_member(update: Update, context: CallbackContext):
    logger.info("Handling left chat member event...")
    left_member = update.message.left_chat_member
    telegram_id = left_member.id
    user_data = users_collection.find_one({"telegram_id": telegram_id})
    if user_data and user_data.get("user_id") is not None:
        code, mention, result = await delete_user_by_telegram_id(telegram_id, context)
        # 发送通知消息到群里
        if code == 200:
            notification_message = f"检测到这虎揍 {mention} 退群，账号已自动删除。"
        else:
            notification_message = f"检测到这虎揍 {mention} 退群，但删除账号时出错：{result}"

        logger.info(f"处理用户 {left_member.username} ({telegram_id}) 退出群组事件：{result}")
        for chat_id in ALLOWED_GROUP_IDS:
            await context.bot.send_message(chat_id=chat_id, text=notification_message, parse_mode='HTML')
    if not user_data or user_data.get("user_id") is None:
        users_collection.delete_one({"telegram_id": telegram_id})
        whitelist_collection.delete_one({"telegram_id": telegram_id})
@admin_only
async def delete_not_in_group(update: Update, context: CallbackContext):
    await update.callback_query.answer(cache_time=5)
    await update.callback_query.message.reply_text("正在删除不在群组用户...")
    users = users_collection.find({"telegram_id": {"$ne": None}})
    for user in users:
        if not await is_user_in_allowed_group_id(user["telegram_id"], ALLOWED_GROUP_IDS[0], context):
            text = f"{user['telegram_id']} 不在群组，删掉用户\n"
            if user.get("user_id") is not None:
                await navidrome_service.delete_user(user["user_id"], user.get("username"))
                text += f"{user['telegram_id']} Navidrome账号已删除\n"
            else:
                text += f"{user['telegram_id']} Navidrome账号不存在\n"
            users_collection.delete_one({"telegram_id": user["telegram_id"]})
            whitelist_collection.delete_one({"telegram_id": user["telegram_id"]})
            await update.callback_query.message.reply_text(text)
    await update.callback_query.message.reply_text("执行完成！")