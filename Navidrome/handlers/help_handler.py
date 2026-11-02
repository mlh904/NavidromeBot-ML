from config import ADMIN_ID
from handlers.permissions import private_only
@private_only
async def help(update, context):
    user_id = update.effective_message.from_user.id
    if user_id in ADMIN_ID:
        message = (
            "命令帮助：\n"
            '/help 查看帮助信息 \n'
            '/start 开启主面板 \n'
            '/mm 查看用户信息 \n'
            '/new_code 创建新的兑换码（默认一个，命令+个数） \n'
            '/del_user 回复消息或tgid删除Navirome账号(命令+tgid) \n'
            '/new_line 新增或修改线路(命令+名字+线路) \n'
            '/del_line 删除线路(命令+名字) \n'
            '/na_token 检查所有 Navidrome Token \n'
            '/sync 把账号同步到所有 Navidrome（新加服务器后执行一次） \n'
            '/gd 歌单链接  导入网易云/QQ/酷我/酷狗歌单到所有服务器 \n'
        )
        await update.message.reply_text(message)
