import asyncio
import json
import random
import string

import aiohttp

from config import NAVIDROME_SERVERS
from database import pending_ops_collection, users_collection
from log import logger
from util import get_now_utc


class ResponseCode:
    USERNAME_EXISTS = 5001,
    USER_NOT_FOUND = 5002,
    TOKEN_EXPIRED = 401
    SERVER_ERROR = 500
    RESET_PASSWORD_USER_NOT_FOUND = 5003


error_message = {
    ResponseCode.USERNAME_EXISTS: "虎揍换个用户名！用户名重复啦！",
    ResponseCode.TOKEN_EXPIRED: "Navidrome token 已过期，正在获取新的令牌。",
    ResponseCode.SERVER_ERROR: "服务器内部错误",
    ResponseCode.RESET_PASSWORD_USER_NOT_FOUND: "重置密码失败，用户不存在"
}


class ApiResponse:
    def __init__(self, code, message, data=None):
        self.code = code
        self.message = message
        self.data = data
        self.total = None

    def __repr__(self):
        return f"ApiResponse(code={self.code}, message={self.message!r})"


def is_ok(resp):
    return isinstance(resp.code, int) and 200 <= resp.code < 300


class NavidromeError(Exception):
    """某台 Navidrome 连不上或返回异常。"""


class NavidromeClient:
    """单台 Navidrome 的原生 REST API 封装。"""

    def __init__(self, name, base_url, username, password, timeout=120):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.token = None
        self.session = None
        self._lock = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    @staticmethod
    async def _read_body(resp):
        text = await resp.text()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def login_as(self, username, password):
        """登录指定账号，成功返回登录信息（含 token、id），密码错误返回 None。"""
        session = await self._get_session()
        try:
            async with session.post(f"{self.base_url}/auth/login",
                                    json={"username": username, "password": password}) as resp:
                data = await self._read_body(resp)
                if resp.status == 200 and isinstance(data, dict) and data.get("token"):
                    return data
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise NavidromeError(f"无法连接 {self.name}：{e}") from e

    async def refresh_token(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            try:
                data = await self.login_as(self.username, self.password)
            except NavidromeError as e:
                logger.error(str(e))
                return False
            if data:
                self.token = data["token"]
                logger.info(f"[{self.name}] Navidrome 令牌刷新成功")
                return True
            logger.error(f"[{self.name}] 管理员登录失败，请检查配置里的账号密码")
            return False

    async def request(self, method, endpoint, payload=None, params=None, token=None):
        """token 为空时使用管理员令牌（过期自动刷新）；传入 token 则以该用户身份请求。"""
        use_admin = token is None
        if use_admin and not self.token:
            await self.refresh_token()
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        for attempt in range(2):
            bearer = self.token if use_admin else token
            headers = {"X-Nd-Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
            try:
                async with session.request(method, url, headers=headers, json=payload, params=params) as resp:
                    data = await self._read_body(resp)
                    if resp.status == 401 and use_admin and attempt == 0:
                        if await self.refresh_token():
                            continue
                    ok = 200 <= resp.status < 300
                    result = ApiResponse(resp.status, "请求成功" if ok else f"请求失败({resp.status})", data)
                    total = resp.headers.get("X-Total-Count")
                    if total and total.isdigit():
                        result.total = int(total)
                    return result
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"[{self.name}] {method} {endpoint} 失败：{e}")
                return ApiResponse(ResponseCode.SERVER_ERROR, f"无法连接 {self.name}")
        return ApiResponse(ResponseCode.TOKEN_EXPIRED, f"{self.name} 认证失败")

    # ---------- 用户 ----------
    async def list_users(self):
        resp = await self.request("GET", "/api/user",
                                  params={"_start": 0, "_end": 100000, "_sort": "userName", "_order": "ASC"})
        if is_ok(resp) and isinstance(resp.data, list):
            return resp.data
        raise NavidromeError(f"{self.name} 获取用户列表失败：{resp.message}")

    async def find_user(self, username):
        target = str(username).lower()
        for user in await self.list_users():
            if str(user.get("userName", "")).lower() == target:
                return user
        return None

    async def create_user(self, username, name, password):
        payload = {"isAdmin": False, "userName": username, "name": name or username, "password": password}
        resp = await self.request("POST", "/api/user", payload)
        if is_ok(resp) and isinstance(resp.data, dict) and resp.data.get("id"):
            return resp
        if isinstance(resp.data, dict):
            errors = resp.data.get("errors") or {}
            if errors.get("userName") == "ra.validation.unique":
                return ApiResponse(ResponseCode.USERNAME_EXISTS, error_message[ResponseCode.USERNAME_EXISTS], resp.data)
        logger.error(f"[{self.name}] 创建用户 {username} 失败：{resp.code} {resp.data}")
        return ApiResponse(ResponseCode.SERVER_ERROR, f"{self.name} 创建用户失败", resp.data)

    async def delete_user_by_id(self, user_id):
        return await self.request("DELETE", f"/api/user/{user_id}")

    async def set_password(self, user, name, new_password):
        payload = dict(user)
        payload.update({"name": name or user.get("name"), "changePassword": True, "password": new_password})
        return await self.request("PUT", f"/api/user/{user['id']}", payload)

    # ---------- 歌曲 / 歌单 ----------
    async def list_songs(self, token=None, page_size=2000, retries=2):
        songs, start = [], 0
        while True:
            resp, last_err = None, None
            for attempt in range(retries + 1):
                try:
                    resp = await self.request("GET", "/api/song", params={
                        "_start": start, "_end": start + page_size,
                        "_sort": "title", "_order": "ASC"}, token=token)
                    if not is_ok(resp) or not isinstance(resp.data, list):
                        raise NavidromeError(f"{self.name} 获取歌曲列表失败：{resp.message}")
                    last_err = None
                    break
                except NavidromeError as e:
                    last_err = e
                except Exception as e:  # noqa: BLE001
                    last_err = NavidromeError(f"{self.name} 获取歌曲列表失败：{type(e).__name__}")
                if attempt < retries:
                    wait = 2 * (attempt + 1)
                    logger.warning(f"[{self.name}] 曲库第 {start} 条起拉取失败，{wait} 秒后重试：{last_err}")
                    await asyncio.sleep(wait)
            if last_err is not None:
                raise last_err
            songs.extend(resp.data)
            if len(resp.data) < page_size or (resp.total is not None and len(songs) >= resp.total):
                return songs
            start += page_size

    async def list_playlists(self, token=None):
        resp = await self.request("GET", "/api/playlist", params={"_start": 0, "_end": 100000}, token=token)
        if is_ok(resp) and isinstance(resp.data, list):
            return resp.data
        raise NavidromeError(f"{self.name} 获取歌单失败：{resp.message}")

    async def create_playlist(self, name, comment, token=None, public=False):
        resp = await self.request("POST", "/api/playlist",
                                  {"name": name, "comment": comment, "public": public}, token=token)
        if is_ok(resp) and isinstance(resp.data, dict) and resp.data.get("id"):
            return resp.data["id"]
        raise NavidromeError(f"{self.name} 创建歌单失败：{resp.message} {resp.data}")

    async def add_playlist_tracks(self, playlist_id, song_ids, token=None, chunk=200):
        for i in range(0, len(song_ids), chunk):
            resp = await self.request("POST", f"/api/playlist/{playlist_id}/tracks",
                                      {"ids": song_ids[i:i + chunk]}, token=token)
            if not is_ok(resp):
                raise NavidromeError(f"{self.name} 添加歌曲到歌单失败：{resp.message} {resp.data}")

    async def update_playlist(self, playlist_id, payload, token=None):
        return await self.request("PUT", f"/api/playlist/{playlist_id}", payload, token=token)

    async def delete_playlist(self, playlist_id, token=None):
        return await self.request("DELETE", f"/api/playlist/{playlist_id}", token=token)

    async def check(self):
        return await self.request("GET", "/api/translation/zh-Hans")


class MultiNavidromeService:
    """把对账号的改动同步到配置里的所有 Navidrome。

    第一台为主服务器：数据库里的 user_id 记的是主服务器上的 ID。
    其它服务器一律按用户名操作；某台暂时连不上时，操作记入 pending_ops，
    由定时任务或管理员 /sync 补做。
    """

    def __init__(self, servers):
        if not servers:
            raise RuntimeError("config.json 里没有配置 NAVIDROME_SERVERS")
        self.clients = [NavidromeClient(s["name"], s["url"], s["username"], s["password"]) for s in servers]
        self.base_url = self.primary.base_url
        self._sync_lock = None

    @property
    def primary(self):
        return self.clients[0]

    def get_client(self, name):
        for c in self.clients:
            if c.name == name:
                return c
        return None

    def generate_random_password(self, length=10):
        characters = string.ascii_letters + string.digits
        return ''.join(random.SystemRandom().choice(characters) for _ in range(length))

    # ---------- 待同步队列 ----------
    @staticmethod
    def _set_pending(server, op, username, error=None):
        pending_ops_collection.update_one(
            {"server": server, "username": str(username).lower()},
            {"$set": {"op": op, "display_username": username, "updated_at": get_now_utc(), "last_error": error},
             "$setOnInsert": {"created_at": get_now_utc()}},
            upsert=True)
        logger.warning(f"[{server}] {op} {username} 未完成，已加入待同步队列：{error}")

    @staticmethod
    def _clear_pending(server, username):
        pending_ops_collection.delete_one({"server": server, "username": str(username).lower()})

    async def _delete_on(self, client, username):
        """确保该服务器上没有此用户。返回 (成功?, 错误信息)。管理员账号永远不删。"""
        try:
            user = await client.find_user(username)
        except NavidromeError as e:
            return False, str(e)
        if user is None:
            return True, None
        if user.get("isAdmin"):
            logger.warning(f"[{client.name}] 跳过删除管理员账号 {username}")
            return True, None
        resp = await client.delete_user_by_id(user["id"])
        return (True, None) if is_ok(resp) else (False, resp.message)

    async def _ensure_on(self, client, username, name, password, allow_create=True):
        """确保该服务器上有此用户且密码为 password。返回 (成功?, 远端用户, 错误信息)。"""
        try:
            user = await client.find_user(username)
        except NavidromeError as e:
            return False, None, str(e)
        if user is None:
            if not allow_create:
                return False, None, "账号不存在"
            resp = await client.create_user(username, name, password)
            return (True, resp.data, None) if is_ok(resp) else (False, None, resp.message)
        # 机器人自己用来登录的账号不能改密码，否则会立刻失去所有线路的访问权限
        if str(username).lower() == str(client.username).lower():
            return False, user, "这是机器人自己使用的账号，改密码会导致机器人失联"

        # 管理员账号同样允许改密码（原先这里直接跳过，导致“提示成功但没改”）
        resp = await client.set_password(user, name, password)
        if not is_ok(resp):
            return False, user, resp.message
        # 改完立刻验证一次，避免接口返回 2xx 但实际没生效
        try:
            check = await client.find_user(username)
            if check is not None:
                ok_login = await client.login_as(username, password)
                if not ok_login:
                    return False, user, "密码未生效"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{client.name}] 校验新密码失败：{type(e).__name__}")
        return True, user, None

    # ---------- 对外接口（与原版同名，handlers 无需大改） ----------
    async def refresh_bearer_token(self):
        results = await asyncio.gather(*(c.refresh_token() for c in self.clients))
        return all(results)

    async def check_token(self):
        lines, all_ok = [], True
        for c in self.clients:
            resp = await c.check()
            ok = is_ok(resp)
            all_ok = all_ok and ok
            lines.append(f"{'✅' if ok else '❌'} {c.name}：{'正常' if ok else resp.message}")
        return ApiResponse(200 if all_ok else ResponseCode.SERVER_ERROR, "\n".join(lines))

    async def get_users(self):
        return await self.primary.request("GET", "/api/user", params={"_start": 0, "_end": 100000})

    async def get_user(self, user_id):
        return await self.primary.request("GET", f"/api/user/{user_id}")

    async def create_na_user(self, username, name, password):
        """在所有服务器上创建同名同密码账号；任何一台失败都会撤销已建好的，保证一致。"""
        for c in self.clients:
            try:
                if await c.find_user(username):
                    return ApiResponse(ResponseCode.USERNAME_EXISTS, error_message[ResponseCode.USERNAME_EXISTS])
            except NavidromeError as e:
                logger.error(str(e))
                return ApiResponse(ResponseCode.SERVER_ERROR, f"{c.name} 暂时连不上，请稍后再注册。")
        created, ids = [], {}
        for c in self.clients:
            resp = await c.create_user(username, name, password)
            if is_ok(resp):
                created.append((c, resp.data["id"]))
                ids[c.name] = resp.data["id"]
                continue
            for done_client, uid in created:
                rollback = await done_client.delete_user_by_id(uid)
                if not is_ok(rollback):
                    self._set_pending(done_client.name, "delete", username, rollback.message)
            if resp.code == ResponseCode.USERNAME_EXISTS:
                return resp
            return ApiResponse(ResponseCode.SERVER_ERROR,
                               f"在 {c.name} 创建账号失败，已撤销其它服务器上的账号，请稍后重试。")
        pending_ops_collection.delete_many({"username": str(username).lower()})
        logger.info(f"已在 {len(self.clients)} 台服务器创建用户 {username}")
        return ApiResponse(200, "创建成功", {"id": ids[self.primary.name], "na_ids": ids})

    async def delete_user(self, user_id=None, username=None):
        if not username and user_id:
            resp = await self.get_user(user_id)
            if is_ok(resp) and isinstance(resp.data, dict):
                username = resp.data.get("userName")
        if not username:
            return ApiResponse(ResponseCode.USER_NOT_FOUND, "找不到这个账号的用户名")
        failed = []
        for c in self.clients:
            ok, err = await self._delete_on(c, username)
            if ok:
                self._clear_pending(c.name, username)
            else:
                failed.append(c.name)
                self._set_pending(c.name, "delete", username, err)
        if failed:
            return ApiResponse(200, f"已删除；{'、'.join(failed)} 暂时连不上，稍后自动补删", {"failed": failed})
        return ApiResponse(200, "删除成功", {"failed": []})

    async def reset_password(self, user_id, name, username, new_password):
        failed, reasons, primary_ok = [], {}, False
        for c in self.clients:
            ok, _, err = await self._ensure_on(c, username, name, new_password, allow_create=False)
            if ok:
                self._clear_pending(c.name, username)
                primary_ok = primary_ok or c is self.primary
            else:
                failed.append(c.name)
                if err:
                    reasons[c.name] = err
                self._set_pending(c.name, "password", username, err)

        if not primary_ok:
            # 优先回传真实原因，而不是一律说“连不上”
            why = reasons.get(self.primary.name) or next(iter(reasons.values()), "")
            if why:
                return ApiResponse(ResponseCode.SERVER_ERROR, f"重置密码失败：{why}")
            return ApiResponse(ResponseCode.SERVER_ERROR,
                               f"重置密码失败：{self.primary.name} 暂时连不上，请稍后再试")

        msg = "重置成功" if not failed else f"重置成功；{'、'.join(failed)} 暂时连不上，稍后自动补同步"
        return ApiResponse(200, msg, {"failed": failed})

    # ---------- 补同步 ----------
    async def process_pending(self):
        """执行待同步队列，返回 (完成数, 仍失败数)。"""
        done = failed = 0
        for op in list(pending_ops_collection.find({})):
            client = self.get_client(op.get("server"))
            if client is None:
                pending_ops_collection.delete_one({"_id": op["_id"]})
                continue
            uname = op.get("display_username") or op["username"]
            if op.get("op") == "delete":
                ok, err = await self._delete_on(client, uname)
            else:
                doc = users_collection.find_one({"username": uname, "user_id": {"$ne": None}})
                if doc is None:
                    pending_ops_collection.delete_one({"_id": op["_id"]})
                    continue
                ok, _, err = await self._ensure_on(client, uname, doc.get("name") or uname, doc.get("password"))
            if ok:
                pending_ops_collection.delete_one({"_id": op["_id"]})
                done += 1
            else:
                pending_ops_collection.update_one({"_id": op["_id"]},
                                                  {"$set": {"last_error": err, "updated_at": get_now_utc()}})
                failed += 1
        return done, failed

    async def full_sync(self):
        """补做待同步队列，并确保数据库里的每个账号在每台服务器上都存在（新加服务器后执行一次即可）。"""
        if self._sync_lock is None:
            self._sync_lock = asyncio.Lock()
        async with self._sync_lock:
            pending_done, pending_failed = await self.process_pending()
            db_users = list(users_collection.find({"user_id": {"$ne": None}, "username": {"$ne": None}}))
            pending_deletes = {(o["server"], o["username"]) for o in pending_ops_collection.find({"op": "delete"})}
            servers = []
            for c in self.clients:
                stat = {"name": c.name, "ok": True, "created": 0, "exists": 0, "failed": 0, "error": None}
                try:
                    remote = {str(u.get("userName", "")).lower(): u for u in await c.list_users()}
                except NavidromeError as e:
                    stat.update(ok=False, error=str(e))
                    servers.append(stat)
                    continue
                for doc in db_users:
                    uname = doc["username"]
                    if (c.name, uname.lower()) in pending_deletes:
                        continue
                    remote_user = remote.get(uname.lower())
                    if remote_user is None:
                        password = doc.get("password")
                        if not password:
                            password = self.generate_random_password()
                            users_collection.update_one({"_id": doc["_id"]}, {"$set": {"password": password}})
                        resp = await c.create_user(uname, doc.get("name") or uname, password)
                        if not is_ok(resp):
                            stat["failed"] += 1
                            self._set_pending(c.name, "create", uname, resp.message)
                            continue
                        stat["created"] += 1
                        remote_user = resp.data
                    else:
                        stat["exists"] += 1
                    if c is self.primary and str(doc.get("user_id")) != str(remote_user.get("id")):
                        users_collection.update_one({"_id": doc["_id"]}, {"$set": {"user_id": str(remote_user["id"])}})
                servers.append(stat)
            return {"pending_done": pending_done, "pending_failed": pending_failed, "servers": servers}


navidrome_service = MultiNavidromeService(NAVIDROME_SERVERS)
