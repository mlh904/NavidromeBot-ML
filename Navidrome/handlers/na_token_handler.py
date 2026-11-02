from telegram import Update
from telegram.ext import CallbackContext
import config
from services.navidrome_client import navidrome_service
from handlers.permissions import admin_only, private_only
from log import logger


# 检查 Navidrome 令牌的函数


@admin_only
@private_only
async def na_token(update: Update, context: CallbackContext):
    await navidrome_service.refresh_bearer_token()
    result = await navidrome_service.check_token()

    logger.info(f"Token check response: Code {result.code}, Message: {result.message}")

    if result.code == 200:  # 所有服务器令牌都有效
        await update.message.reply_text(f"Navidrome token 全部有效：\n{result.message}")
        logger.info("bearer token is valid.")
    else:
        await update.message.reply_text(f"Navidrome token 检查有失败：\n{result.message}")
        logger.error(f"Navidrome token check failed, status code: {result.code}, message: {result.message}")
