"""从网易云音乐 / QQ音乐 / 酷我音乐 / 酷狗音乐 抓取歌单。

这些平台都没有公开 API，下面用的是它们网页/客户端在用的接口，
平台改版后可能需要跟着调整。抓取结果统一为：
    {"platform": "网易云音乐", "source_id": "123", "name": "歌单名",
     "tracks": [{"title": "歌名", "artists": ["歌手"], "album": "专辑", "duration": 秒}]}
"""
import asyncio
import json
import re
from urllib.parse import urlparse, parse_qs, unquote

import aiohttp

from config import NETEASE_COOKIE, QQ_COOKIE, PLAYLIST_MAX_SONGS
from log import logger

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

URL_RE = re.compile(r"https?://[^\s<>\"'，。！？）】]+", re.I)
ARTIST_SPLIT = re.compile(r"\s*(?:/|;|；|、|&|，|,|\+|\bfeat\.?\b|\bft\.?\b)\s*", re.I)


class PlaylistError(Exception):
    """给用户看的错误信息。"""


def split_artists(text):
    if not text:
        return []
    return [a.strip() for a in ARTIST_SPLIT.split(str(text)) if a and a.strip()]


def _track(title, artists, album=None, duration=None):
    title = (title or "").strip()
    if not title:
        return None
    try:
        duration = int(float(duration)) if duration not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    if duration and duration > 36000:  # 毫秒
        duration //= 1000
    return {"title": title, "artists": [a for a in artists if a], "album": (album or "").strip(), "duration": duration}


class PlaylistFetcher:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=30)

    async def _get(self, session, url, headers=None, params=None, as_json=True):
        h = {"User-Agent": UA}
        h.update(headers or {})
        async with session.get(url, headers=h, params=params) as resp:
            text = await resp.text(errors="replace")
            if resp.status >= 400:
                raise PlaylistError(f"平台返回错误 {resp.status}")
            if not as_json:
                return text
            return self._loads(text)

    async def _post(self, session, url, data, headers=None):
        h = {"User-Agent": UA}
        h.update(headers or {})
        async with session.post(url, headers=h, data=data) as resp:
            text = await resp.text(errors="replace")
            if resp.status >= 400:
                raise PlaylistError(f"平台返回错误 {resp.status}")
            return self._loads(text)

    @staticmethod
    def _loads(text):
        text = text.strip()
        m = re.match(r"^[\w$.]+\((.*)\)\s*;?$", text, re.S)  # JSONP
        if m:
            text = m.group(1)
        try:
            return json.loads(text)
        except ValueError:
            raise PlaylistError("平台返回的数据无法解析，可能接口已变动")

    async def _resolve(self, session, url):
        """跟随短链接跳转，返回最终 URL 和页面内容。"""
        try:
            async with session.get(url, headers={"User-Agent": MOBILE_UA}, allow_redirects=True) as resp:
                body = await resp.text(errors="replace")
                return str(resp.url), body
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise PlaylistError(f"打开链接失败：{e}")

    # ---------- 识别链接 ----------
    @staticmethod
    def _detect(url):
        host = (urlparse(url).netloc or "").lower()
        if "163" in host and ("music" in host or "163cn" in host):
            return "netease"
        if "qq.com" in host:
            return "qq"
        if "kuwo" in host:
            return "kuwo"
        if "kugou" in host:
            return "kugou"
        return None

    @staticmethod
    def _qs(url):
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        frag = parsed.fragment
        if "?" in frag:
            qs.update(parse_qs(frag.split("?", 1)[1]))
        return qs

    def _netease_id(self, url):
        qs = self._qs(url)
        if qs.get("id"):
            return qs["id"][0]
        m = re.search(r"playlist[/=](\d+)", url)
        return m.group(1) if m else None

    def _qq_id(self, url):
        qs = self._qs(url)
        for key in ("id", "disstid", "dissid"):
            if qs.get(key) and qs[key][0].isdigit():
                return qs[key][0]
        m = re.search(r"playlist/(\d+)", url)
        return m.group(1) if m else None

    def _kuwo_id(self, url):
        m = re.search(r"playlist(?:_detail)?/(\d+)", url)
        if m:
            return m.group(1)
        qs = self._qs(url)
        for key in ("pid", "id"):
            if qs.get(key) and qs[key][0].isdigit():
                return qs[key][0]
        return None

    async def fetch(self, text):
        m = URL_RE.search(text or "")
        if not m:
            raise PlaylistError("没有找到链接。用法：/gd 歌单链接")
        url = m.group(0).rstrip(".,;")
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            platform = self._detect(url)
            body = ""
            if platform is None or platform in ("netease", "qq", "kugou", "kuwo"):
                # 短链接需要先跳转一次才能拿到歌单 ID
                needs_resolve = (platform is None or "163cn" in url or "/u?" in url or "fcgi-bin/u" in url
                                 or "t1.kugou" in url or "t.kugou" in url or "kuwo.cn/s/" in url)
                if needs_resolve:
                    url, body = await self._resolve(session, url)
                    platform = self._detect(url)
            if platform == "netease":
                pid = self._netease_id(url)
                if not pid:
                    raise PlaylistError("没识别出网易云歌单 ID，请发送歌单分享链接")
                result = await self._netease(session, pid)
            elif platform == "qq":
                pid = self._qq_id(url)
                if not pid:
                    raise PlaylistError("没识别出 QQ 音乐歌单 ID，请发送歌单分享链接")
                result = await self._qq(session, pid)
            elif platform == "kuwo":
                pid = self._kuwo_id(url)
                if not pid:
                    raise PlaylistError("没识别出酷我歌单 ID，请发送歌单分享链接")
                result = await self._kuwo(session, pid)
            elif platform == "kugou":
                result = await self._kugou(session, url, body)
            else:
                raise PlaylistError("目前只支持网易云音乐、QQ音乐、酷我音乐、酷狗音乐的歌单链接")
        tracks = [t for t in result["tracks"] if t]
        if not tracks:
            raise PlaylistError("歌单是空的，或者是私密歌单抓不到")
        if len(tracks) > PLAYLIST_MAX_SONGS:
            tracks = tracks[:PLAYLIST_MAX_SONGS]
            result["truncated"] = True
        result["tracks"] = tracks
        logger.info(f"抓取歌单 {result['platform']} {result['source_id']} 「{result['name']}」{len(tracks)} 首")
        return result

    # ---------- 网易云 ----------
    async def _netease(self, session, pid):
        headers = {"Referer": "https://music.163.com/", "Cookie": "os=pc; " + (NETEASE_COOKIE or "")}
        data = await self._post(session, "https://music.163.com/api/v6/playlist/detail",
                                {"id": pid, "n": 100000, "s": 8}, headers)
        if data.get("code") != 200 or not data.get("playlist"):
            if data.get("code") in (401, 404, 20001):
                raise PlaylistError("网易云歌单不存在或是私密歌单")
            raise PlaylistError(f"网易云返回异常：{data.get('message') or data.get('code')}")
        pl = data["playlist"]
        ids = [t["id"] for t in pl.get("trackIds") or []][:PLAYLIST_MAX_SONGS]
        songs = {}
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            detail = await self._post(session, "https://music.163.com/api/v3/song/detail",
                                      {"c": json.dumps([{"id": x} for x in batch])}, headers)
            for s in detail.get("songs") or []:
                songs[s["id"]] = s
        if not songs:  # 详情接口被限制时退回歌单自带的曲目
            songs = {s["id"]: s for s in pl.get("tracks") or []}
            ids = [s["id"] for s in pl.get("tracks") or []]
        tracks = []
        for sid in ids:
            s = songs.get(sid)
            if s:
                tracks.append(_track(s.get("name"), [a.get("name") for a in s.get("ar") or []],
                                     (s.get("al") or {}).get("name"), s.get("dt")))
        return {"platform": "网易云音乐", "source_id": str(pid), "name": pl.get("name") or f"网易云歌单{pid}",
                "tracks": tracks}

    # ---------- QQ 音乐 ----------
    async def _qq(self, session, pid):
        headers = {"Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"}
        if QQ_COOKIE:
            headers["Cookie"] = QQ_COOKIE
        tracks, name, begin, step = [], None, 0, 1000
        while begin < PLAYLIST_MAX_SONGS:
            params = {"type": 1, "json": 1, "utf8": 1, "onlysong": 0, "new_format": 1, "disstid": pid,
                      "loginUin": 0, "hostUin": 0, "format": "json", "inCharset": "utf8", "outCharset": "utf-8",
                      "notice": 0, "platform": "yqq.json", "needNewCode": 0,
                      "song_begin": begin, "song_num": step}
            data = await self._get(session, "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg",
                                   headers, params)
            cdlist = data.get("cdlist") or []
            if data.get("code") not in (0, None) or not cdlist:
                if begin == 0:
                    raise PlaylistError("QQ 音乐歌单不存在或是私密歌单")
                break
            cd = cdlist[0]
            name = name or cd.get("dissname")
            batch = cd.get("songlist") or []
            for s in batch:
                tracks.append(_track(s.get("title") or s.get("name"), [a.get("name") for a in s.get("singer") or []],
                                     (s.get("album") or {}).get("name"), s.get("interval")))
            total = int(cd.get("total_song_num") or cd.get("songnum") or 0)
            begin += step
            if len(batch) < step or (total and begin >= total):
                break
        return {"platform": "QQ音乐", "source_id": str(pid), "name": name or f"QQ歌单{pid}", "tracks": tracks}

    # ---------- 酷我 ----------
    async def _kuwo(self, session, pid):
        tracks, name, page, size = [], None, 0, 100
        while len(tracks) < PLAYLIST_MAX_SONGS:
            params = {"op": "getlistinfo", "pid": pid, "pn": page, "rn": size, "encode": "utf8",
                      "keyset": "pl2012", "identity": "kuwo", "pcmp4": 1, "vipver": "MUSIC_9.0.5.0_W1", "newver": 1}
            data = await self._get(session, "http://nplserver.kuwo.cn/pl.svc", None, params)
            batch = data.get("musiclist") or []
            if page == 0:
                if not batch and not data.get("title"):
                    raise PlaylistError("酷我歌单不存在或是私密歌单")
                name = data.get("title")
            for s in batch:
                tracks.append(_track(unquote(s.get("name") or ""), split_artists(unquote(s.get("artist") or "")),
                                     unquote(s.get("album") or ""), s.get("duration")))
            total = int(data.get("total") or 0)
            page += 1
            if len(batch) < size or (total and page * size >= total):
                break
        return {"platform": "酷我音乐", "source_id": str(pid), "name": name or f"酷我歌单{pid}", "tracks": tracks}

    # ---------- 酷狗 ----------
    @staticmethod
    def _kugou_filename(filename):
        """酷狗的 filename 形如 “歌手 - 歌名”。"""
        filename = re.sub(r"\.(mp3|flac|m4a|wav|ape)$", "", filename or "", flags=re.I)
        if " - " in filename:
            artist, title = filename.split(" - ", 1)
            return title, split_artists(artist)
        return filename, []

    async def _kugou(self, session, url, body):
        m = re.search(r"special/single/(\d+)", url) or re.search(r"[?&](?:specialid|special_id)=(\d+)", url)
        if m:
            return await self._kugou_special(session, m.group(1))
        if not body:
            _, body = await self._resolve(session, url)
        result = self._kugou_from_html(body)
        if result:
            return result
        sid = re.search(r'"specialid"\s*:\s*"?(\d+)', body or "")
        if sid and sid.group(1) != "0":
            return await self._kugou_special(session, sid.group(1))
        raise PlaylistError("这个酷狗歌单链接暂时解析不了，请换用酷狗网页版歌单链接试试")

    async def _kugou_special(self, session, sid):
        info = await self._get(session, "http://mobilecdnbj.kugou.com/api/v3/special/info",
                               None, {"specialid": sid})
        name = ((info.get("data") or {}).get("specialname")) or f"酷狗歌单{sid}"
        tracks, page, size = [], 1, 300
        while len(tracks) < PLAYLIST_MAX_SONGS:
            data = await self._get(session, "http://mobilecdnbj.kugou.com/api/v3/special/song", None,
                                   {"specialid": sid, "page": page, "pagesize": size, "plat": 2, "version": 8000})
            d = data.get("data") or {}
            batch = d.get("info") or []
            if page == 1 and not batch:
                raise PlaylistError("酷狗歌单不存在或是私密歌单")
            for s in batch:
                title, artists = self._kugou_filename(s.get("filename"))
                tracks.append(_track(title, artists, s.get("album_name") or s.get("remark"), s.get("duration")))
            total = int(d.get("total") or 0)
            if len(batch) < size or (total and page * size >= total):
                break
            page += 1
        return {"platform": "酷狗音乐", "source_id": str(sid), "name": name, "tracks": tracks}

    def _kugou_from_html(self, body):
        """酷狗分享页把歌曲列表直接写在页面里（dataFromSmarty / 列表 JSON）。"""
        if not body:
            return None
        songs = []
        m = re.search(r"dataFromSmarty\s*=\s*(\[.*?\])\s*[,;]\s*\n", body, re.S)
        if m:
            try:
                for s in json.loads(m.group(1)):
                    title = s.get("song_name") or s.get("songname")
                    artists = split_artists(s.get("author_name") or s.get("singername"))
                    if not title and s.get("filename"):
                        title, artists = self._kugou_filename(s.get("filename"))
                    songs.append(_track(title, artists, s.get("album_name"), s.get("timelength") or s.get("duration")))
            except ValueError:
                songs = []
        if not songs:
            for fn in re.findall(r'"(?:filename|FileName)"\s*:\s*"((?:[^"\\]|\\.)*)"', body):
                try:
                    fn = json.loads(f'"{fn}"')
                except ValueError:
                    pass
                title, artists = self._kugou_filename(fn)
                songs.append(_track(title, artists))
        songs = [s for s in songs if s]
        if not songs:
            return None
        name = None
        for pat in (r'"specialname"\s*:\s*"((?:[^"\\]|\\.)*)"', r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"intro"',
                    r"<title>([^<]+)</title>"):
            mm = re.search(pat, body)
            if mm:
                name = mm.group(1)
                try:
                    name = json.loads(f'"{name}"')
                except ValueError:
                    pass
                name = re.sub(r"\s*[-_|].*酷狗.*$", "", name).strip()
                break
        gid = re.search(r"(collection_[\w]+|gcid_\w+)", body)
        return {"platform": "酷狗音乐", "source_id": gid.group(1) if gid else "html",
                "name": name or "酷狗歌单", "tracks": songs}


playlist_fetcher = PlaylistFetcher()
