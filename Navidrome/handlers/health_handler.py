from telegram import Update
from telegram.ext import ContextTypes
from handlers.permissions import admin_only, private_only
from services.navidrome_client import navidrome_service
from database import pending_ops_collection


@admin_only
@private_only
async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.effective_message.reply_text("正在检查 Navidrome 线路…")
    try:
        checks = []
        for c in navidrome_service.clients:
            ok = await c.refresh_token()
            if ok:
                resp = await c.check()
                ok = resp.code == 200
                detail = "正常" if ok else resp.message
            else:
                detail = "管理员 API 登录失败"
            checks.append(f"{'✅' if ok else '❌'} {c.name}：{detail}")
        pending = pending_ops_collection.count_documents({})
        checks.append(f"待补同步操作：{pending} 项")
        await msg.edit_text("🩺 服务自检\\n" + "\\n".join(checks))
    except Exception as e:
        await msg.edit_text(f"❌ 自检失败：{e}")
