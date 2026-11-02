import asyncio
import html

from telegram import Update
from telegram.ext import CallbackContext

from database import users_collection, playlist_imports_collection
from handlers.permissions import private_only
from log import logger
from services.playlist_fetcher import playlist_fetcher, PlaylistError
from services.playlist_importer import playlist_importer
from util import get_now_utc

_running = set()  # 正在导入的 telegram_id，防止同一个人连发
MAX_MISSING_SHOWN = 30


def _fmt_track(t):
    artists = "/".join(t.get("artists") or [])
    return f"{t['title']} - {artists}" if artists else t["title"]


@private_only
async def gd(update: Update, context: CallbackContext):
    tgid = update.effective_user.id
    user = users_collection.find_one({"telegram_id": tgid, "user_id": {"$ne": None}})
    if not user:
        await update.message.reply_text("虎揍先注册账号，才能导入歌单哦！")
        return
    text = " ".join(context.args) if context.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    if not text:
        await update.message.reply_text(
            "用法：/gd 歌单链接\n支持网易云音乐、QQ音乐、酷我音乐、酷狗音乐的歌单分享链接。")
        return
    if tgid in _running:
        await update.message.reply_text("上一个歌单还在导入中，请等它完成再发。")
        return
    _running.add(tgid)
    status = await update.message.reply_text("⏳ 正在读取歌单…")
    try:
        try:
            playlist = await playlist_fetcher.fetch(text)
        except PlaylistError as e:
            await status.edit_text(f"❌ {e}")
            return
        total = len(playlist["tracks"])
        await status.edit_text(
            f"🎵 {playlist['platform']}「{playlist['name']}」共 {total} 首\n⏳ 正在匹配曲库并创建歌单…")
        results = await playlist_importer.import_all(user["username"], user.get("password"), playlist)

        ok_results = [r for r in results if r["ok"]]
        lines = [f"🎵 {html.escape(playlist['platform'])}「{html.escape(playlist['name'])}」"]
        if playlist.get("truncated"):
            lines.append(f"（歌单太长，只导入前 {total} 首）")
        for r in results:
            if r["ok"]:
                tip = "，已覆盖同名旧歌单" if r["replaced"] else ""
                lines.append(f"✅ {html.escape(r['server'])}：匹配 {r['matched']}/{r['total']} 首{tip}")
            else:
                lines.append(f"❌ {html.escape(r['server'])}：{html.escape(r['error'] or '失败')}")
        # 未匹配列表：以主服务器的结果为准（各台曲库可能不同）
        ref = ok_results[0] if ok_results else None
        if ref and ref["missing"]:
            lines.append(f"\n未匹配到的歌曲（{len(ref['missing'])} 首，以 {html.escape(ref['server'])} 为准）：")
            for t in ref["missing"][:MAX_MISSING_SHOWN]:
                lines.append(f"· {html.escape(_fmt_track(t))}")
            if len(ref["missing"]) > MAX_MISSING_SHOWN:
                lines.append(f"…还有 {len(ref['missing']) - MAX_MISSING_SHOWN} 首")
        if not ok_results:
            lines.append("\n歌单没有建成功，请稍后再试。")
        await status.edit_text("\n".join(lines), parse_mode="HTML")

        playlist_imports_collection.insert_one({
            "telegram_id": tgid,
            "username": user["username"],
            "platform": playlist["platform"],
            "source_id": playlist["source_id"],
            "name": playlist["name"],
            "total": total,
            "results": [{k: r.get(k) for k in ("server", "ok", "matched", "error")} for r in results],
            "created_at": get_now_utc(),
        })
    except Exception as e:
        logger.exception("导入歌单失败")
        try:
            await status.edit_text(f"❌ 导入失败：{e}")
        except Exception:
            pass
    finally:
        _running.discard(tgid)
