"""把抓到的歌单曲目匹配到 Navidrome 曲库，并在每台服务器上建歌单。"""
import asyncio
import re
import time
import unicodedata
from difflib import SequenceMatcher

from config import PLAYLIST_MATCH_THRESHOLD
from log import logger
from services.navidrome_client import navidrome_service, NavidromeError

try:
    from opencc import OpenCC
    _cc = OpenCC("t2s")
except Exception:  # opencc 不可用时只做基础归一化
    _cc = None

BRACKETS = re.compile(r"[\(（\[【〔「『<《][^\)）\]】〕」』>》]*[\)）\]】〕」』>》]")
VERSION_WORDS = re.compile(
    r"\b(live|remix|remaster(ed)?|version|ver|edit|mix|demo|acoustic|instrumental|伴奏|现场|版|原唱|女声|男声|cover)\b",
    re.I)
FEAT = re.compile(r"\s*[-\s]?\s*(feat\.?|ft\.?|with)\s+.*$", re.I)
PUNCT = re.compile(r"[\s\-_.,，。!！?？'’\"“”:：;；/\\|·•~～&＆+＋*]+")
ARTIST_SPLIT = re.compile(r"\s*(?:/|;|；|、|&|，|,|\+|\bfeat\.?\b|\bft\.?\b|\band\b|和)\s*", re.I)
CACHE_TTL = 600


def to_simplified(text):
    text = unicodedata.normalize("NFKC", text or "")
    if _cc:
        try:
            text = _cc.convert(text)
        except Exception:
            pass
    return text.lower()


def norm_title(text, keep_brackets=False):
    t = to_simplified(text)
    if not keep_brackets:
        t = BRACKETS.sub(" ", t)
        t = FEAT.sub("", t)
    t = PUNCT.sub("", t)
    return t


def norm_artists(text_or_list):
    if isinstance(text_or_list, (list, tuple)):
        parts = []
        for a in text_or_list:
            parts.extend(ARTIST_SPLIT.split(a or ""))
    else:
        parts = ARTIST_SPLIT.split(text_or_list or "")
    return {PUNCT.sub("", to_simplified(p)) for p in parts if p and PUNCT.sub("", to_simplified(p))}


def ratio(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class SongIndex:
    """一台服务器（某用户可见范围内）的曲库索引。"""

    def __init__(self, songs):
        self.by_title = {}
        self.songs = []
        for s in songs:
            title = s.get("title") or ""
            artists = set()
            for field in ("artist", "albumArtist"):
                artists |= norm_artists(s.get(field))
            for p in (s.get("participants") or {}).get("artist", []) or []:
                artists |= norm_artists(p.get("name"))
            entry = {
                "id": s.get("id"),
                "title_full": norm_title(title, keep_brackets=True),
                "title": norm_title(title),
                "artists": artists,
                "album": norm_title(s.get("album")),
                "duration": s.get("duration") or 0,
            }
            if not entry["id"] or not entry["title"]:
                continue
            self.songs.append(entry)
            self.by_title.setdefault(entry["title"], []).append(entry)

    def _score(self, track, cand, t_title):
        title_score = 1.0 if cand["title"] == t_title else ratio(cand["title"], t_title)
        t_artists = track["_artists"]
        if t_artists and cand["artists"]:
            overlap = len(t_artists & cand["artists"])
            if overlap:
                artist_score = 1.0
            else:
                artist_score = max(ratio(a, b) for a in t_artists for b in cand["artists"])
        else:
            artist_score = 0.6  # 某一边没有歌手信息时，主要靠歌名
        score = title_score * 0.65 + artist_score * 0.35
        if track["_full"] and track["_full"] == cand["title_full"]:
            score += 0.04
        if track.get("album") and cand["album"] and norm_title(track["album"]) == cand["album"]:
            score += 0.03
        d1, d2 = track.get("duration") or 0, cand["duration"] or 0
        if d1 and d2:
            diff = abs(d1 - d2)
            if diff <= 3:
                score += 0.03
            elif diff > 30:
                score -= 0.08
        return score, title_score, artist_score

    def match(self, track):
        t_title = norm_title(track["title"])
        if not t_title:
            return None
        track["_artists"] = norm_artists(track.get("artists") or [])
        track["_full"] = norm_title(track["title"], keep_brackets=True)
        candidates = list(self.by_title.get(t_title, []))
        if not candidates:
            # 歌名完全一致的没有，再做模糊比较（只比较首字符相同或包含关系的，控制开销）
            for e in self.songs:
                if e["title"] and (e["title"][0] == t_title[0] or t_title in e["title"] or e["title"] in t_title):
                    candidates.append(e)
        best, best_score = None, 0.0
        for cand in candidates:
            score, title_score, artist_score = self._score(track, cand, t_title)
            if title_score < 0.75:
                continue
            if track["_artists"] and cand["artists"] and artist_score < 0.5 and title_score < 1.0:
                continue
            if score > best_score:
                best, best_score = cand, score
        if best and best_score >= PLAYLIST_MATCH_THRESHOLD:
            return best["id"]
        return None


class PlaylistImporter:
    def __init__(self):
        self._index_cache = {}  # (服务器名, 用户名) -> (时间, SongIndex)
        self._locks = {}

    async def _index_for(self, client, token, username):
        key = (client.name, username.lower())
        cached = self._index_cache.get(key)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._index_cache.get(key)
            if cached and time.time() - cached[0] < CACHE_TTL:
                return cached[1]
            songs = await client.list_songs(token=token)
            index = SongIndex(songs)
            self._index_cache[key] = (time.time(), index)
            logger.info(f"[{client.name}] 为 {username} 建立曲库索引：{len(index.songs)} 首")
            return index

    async def import_to_server(self, client, username, password, playlist, tracks):
        """在一台服务器上以该用户身份建歌单。返回该台的结果字典。"""
        result = {"server": client.name, "ok": False, "matched": 0, "total": len(tracks),
                  "missing": [], "error": None, "replaced": False}
        try:
            login = await client.login_as(username, password)
        except NavidromeError as e:
            result["error"] = str(e)
            return result
        if not login:
            result["error"] = "账号在这台服务器上登录失败，可让管理员执行 /sync 后重试"
            return result
        token = login["token"]
        try:
            index = await self._index_for(client, token, username)
            song_ids, seen = [], set()
            for t in tracks:
                sid = index.match(dict(t))
                if sid:
                    if sid not in seen:
                        seen.add(sid)
                        song_ids.append(sid)
                    result["matched"] += 1
                else:
                    result["missing"].append(t)
            name = playlist["name"]
            comment = f"导入自{playlist['platform']} · 歌单ID {playlist['source_id']}"
            existing = [p for p in await client.list_playlists(token=token)
                        if p.get("name") == name and p.get("ownerName", username).lower() == username.lower()]
            for p in existing:
                await client.delete_playlist(p["id"], token=token)
            result["replaced"] = bool(existing)
            pl_id = await client.create_playlist(name, comment, token=token)
            if song_ids:
                await client.add_playlist_tracks(pl_id, song_ids, token=token)
            result["ok"] = True
            result["playlist_id"] = pl_id
        except NavidromeError as e:
            result["error"] = str(e)
        except Exception as e:  # 不让一台的意外错误影响其它服务器
            logger.exception(f"[{client.name}] 导入歌单异常")
            result["error"] = f"导入出错：{e}"
        return result

    async def import_all(self, username, password, playlist):
        tracks = playlist["tracks"]
        tasks = [self.import_to_server(c, username, password, playlist, tracks) for c in navidrome_service.clients]
        return await asyncio.gather(*tasks)


playlist_importer = PlaylistImporter()
