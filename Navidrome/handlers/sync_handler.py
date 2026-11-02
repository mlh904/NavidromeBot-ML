from telegram import Update
from telegram.ext import CallbackContext

from handlers.permissions import admin_only, private_only
from log import logger
from services.navidrome_client import navidrome_service


def format_sync_report(report):
    lines = ["🔄 同步完成"]
    if report["pending_done"] or report["pending_failed"]:
        lines.append(f"待补做的操作：完成 {report['pending_done']}，仍失败 {report['pending_failed']}")
    for s in report["servers"]:
        if not s["ok"]:
            lines.append(f"❌ {s['name']}：{s['error']}")
        else:
            lines.append(f"✅ {s['name']}：已有 {s['exists']}，新建 {s['created']}，失败 {s['failed']}")
    return "\n".join(lines)


@admin_only
@private_only
async def sync_users(update: Update, context: CallbackContext):
    msg = await update.message.reply_text("🔄 正在把账号同步到所有 Navidrome…")
    try:
        report = await navidrome_service.full_sync()
        await msg.edit_text(format_sync_report(report))
    except Exception as e:
        logger.exception("手动同步失败")
        await msg.edit_text(f"❌ 同步失败：{e}")


async def auto_sync_job(context: CallbackContext):
    """定时补做之前因为服务器连不上而没完成的删除/改密码/创建。"""
    try:
        done, failed = await navidrome_service.process_pending()
        if done or failed:
            logger.info(f"自动补同步：完成 {done}，仍失败 {failed}")
    except Exception:
        logger.exception("自动补同步出错")
