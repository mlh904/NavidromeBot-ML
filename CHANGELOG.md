# Changelog

## 0.2.0-ml - 2026-11-12

### 新增：账号绑定 / 解绑

用于补回 Bot 数据备份缺失的账号——当用户已在服务器上注册、但 Bot 库里没有记录时，
可以让用户自行把 Telegram 与已有账号关联起来，无需管理员手工录库。

- `/bind [账号名]` — 绑定已有账号。带参数直接进入密码验证，不带参数先提示输入账号。
  账号会在全部线路中查找，密码通过 `/auth/login` 实际验证后才写入绑定，
  并且**不会被保存到数据库**。已绑定其他 Telegram 的账号会被拒绝，防止抢占。
- `/unbind` — 解除 Telegram 与账号的关联。服务器上的音乐账号**保留**，
  可继续登录网页端，也能随时重新绑定。
- `/mybind` — 查看当前绑定状态。
- 以上三条已加入普通用户的 Bot 命令菜单。

### 修复：重置密码显示成功但实际未生效

- **管理员账号密码无法重置**：`_ensure_on()` 中对 `isAdmin` 用户直接返回成功，
  完全没有调用改密接口，导致提示"重置成功"而密码未变。现已移除该分支，
  管理员与普通用户走相同流程。
- **重置后增加校验**：改密后立即用新密码尝试登录一次，
  若未生效则返回"密码未生效"，不再出现静默假成功。
- **不再为不存在的账号创建用户**：`_ensure_on()` 新增 `allow_create` 参数，
  重置密码时传 `False`，找不到账号直接返回"账号不存在"。
  此前会走 `create_user` 分支，把用户凭空创建出来并报成功。
- **保护机器人自身账号**：对当前线路登录账号（如 `botadmin`）改密会被拒绝，
  否则机器人会立刻失去所有线路的访问权限。
- **错误信息回传真实原因**：`reset_password()` 收集各线路的具体错误，
  优先返回真实失败原因，不再一律提示"暂时连不上"。

### 改进

- `/reset_pw` 相关帮助文案中的示例账号名改为通用占位符。
- 命令菜单补充 `/bind`、`/unbind`、`/mybind`。

### 说明

- 绑定流程仅校验密码，不修改服务器上的任何凭据。
- 解绑为纯数据库操作，不触碰 Navidrome 用户数据。
- 若同一账号在不同线路存在，绑定会记录首次命中的用户 ID，
  并在结果中提示该账号存在于哪些线路。

## 0.1.0-ml - 2026-11-02

- Added multi-Navidrome server configuration.
- Added synchronized registration, deletion and password reset flows.
- Added pending operations and `/sync`.
- Added `/health`.
- Added `/gd` playlist import flow.
- Added NetEase, QQ Music, Kuwo and Kugou playlist adapters.
- Added song matching by title, artist, album and duration.
- Added safe configuration templates and public repository documentation.
