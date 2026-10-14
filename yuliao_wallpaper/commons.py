"""Small, cached Wikimedia Commons source. This module never changes wallpaper.

Only manually selected Commons categories are queried: official Featured and
Quality collections plus narrow topics such as electronic components. Those
topic categories do not imply a Featured/Quality certification or a license;
every candidate still needs explicit usable license metadata.
API references: https://www.mediawiki.org/wiki/API:Imageinfo and API:Etiquette.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import threading
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import warnings
from typing import Callable

from PIL import Image, UnidentifiedImageError


API_URL = "https://commons.wikimedia.org/w/api.php"
# A local prototype has no public support URL/account. The UA identifies this
# application and deliberately does not invent somebody else's contact address.
USER_AGENT = "YuliaoWallpaper/0.2.0 (+https://github.com/xieyuqingme-source/yuliao-wallpaper; Windows wallpaper client)"
# Wikimedia moved thumbnails to this separate exact host in 2026:
# https://wikitech.wikimedia.org/wiki/Thumbor
ALLOWED_HOSTS = frozenset({"commons.wikimedia.org", "upload.wikimedia.org", "thumb.wikimedia.org"})
IMAGE_HOSTS = frozenset({"upload.wikimedia.org", "thumb.wikimedia.org"})
THEMES = {
    "landscape": ("Category:Quality images of landscapes", "Category:Quality images of landscapes in Austria"),
    "city": ("Category:Quality images of cityscapes", "Category:Quality images of buildings"),
    "animals": ("Category:Quality images of animals in Canada", "Category:Quality images of birds"),
    "space": ("Category:Featured pictures from NASA", "Category:Hubble images of nebulae", "Category:Featured pictures of astronomy"),
    "technology": ("Category:Focus stacking images of electronic components", "Category:Featured pictures of machines", "Category:Printed circuit boards"),
    "ocean": ("Category:Featured pictures of coasts", "Category:Featured pictures of beaches", "Category:Quality images of coasts"),
    "flowers": ("Category:Featured pictures of flowers", "Category:Quality images of flowers"),
    "mountains": ("Category:Featured pictures of mountains", "Category:Quality images of mountains"),
}
THEME_LABELS = {"landscape": "风景", "city": "城市建筑", "animals": "动物", "space": "太空天文",
                "technology": "科技工程", "ocean": "海洋海岸", "flowers": "花卉", "mountains": "山景"}
QUERY_INTERVAL = 6 * 60 * 60
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CACHE_BYTES = 200 * 1024 * 1024
MAX_JOB_BYTES = 120 * 1024 * 1024
MAX_JOB_CANDIDATES = 240
MAX_JOB_IMAGES = 30
MAX_PIXELS = 25_000_000
MAX_DIMENSION = 8192
MIN_WIDTH, MIN_HEIGHT = 1280, 720
MIME_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
FORMAT_EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
PUBLIC_KEYS = (
    "id", "path", "origin", "title", "theme", "album", "tags", "author",
    "source_url", "license_name", "license_url", "crop_note",
    "themes",
)


class CommonsError(RuntimeError):
    """A bounded, recoverable source/cache error."""


class _Cancelled(CommonsError):
    pass


class _BudgetExceeded(CommonsError):
    pass


class _CacheFull(CommonsError):
    pass


class _TransientError(CommonsError):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def validate_download_url(url: str) -> str:
    """Reject credentials, redirects to foreign hosts, non-TLS and odd ports."""
    if not isinstance(url, str) or len(url) > 16_384 or any(ord(c) < 32 for c in url):
        raise CommonsError("图源地址无效")
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS
                 and parts.port in (None, 443) and not parts.username
                 and not parts.password and not parts.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise CommonsError("图源只允许 Wikimedia 的 HTTPS 地址")
    return url


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style"}:
            self.hidden += 1
        elif tag.lower() in {"br", "p", "div", "li"} and not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif tag.lower() in {"p", "div", "li"} and not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_text(value: object, limit: int = 600) -> str:
    """Treat all source metadata as text, never executable HTML/instructions."""
    if not isinstance(value, str):
        return ""
    parser = _TextOnly()
    parser.feed(value[:8192])
    parser.close()
    result = " ".join("".join(parser.parts).split())
    return "".join(c for c in result if c.isprintable())[:limit]


def _meta(metadata: dict, key: str, limit: int = 600) -> str:
    if not isinstance(metadata, dict):
        return ""
    field = metadata.get(key, {})
    return clean_text(field.get("value", ""), limit) if isinstance(field, dict) else ""


def verified_license(metadata: dict) -> tuple[str, str] | None:
    """Conservative name + official URL match; NC/ND/unknown are skipped.

    Commons sometimes sends HTTP or protocol-relative Creative Commons links.
    They are never fetched: matching exact official paths produces HTTPS links.
    Public-domain metadata without a link must explicitly say Copyrighted=False.
    """
    if not isinstance(metadata, dict):
        return None
    name = _meta(metadata, "LicenseShortName", 80)
    raw_url = _meta(metadata, "LicenseUrl", 300)
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    try:
        url = urlsplit(raw_url)
        safe_cc = (url.scheme in {"http", "https"} and url.hostname == "creativecommons.org"
                   and not url.username and not url.password and url.port in (None, 80, 443)
                   and not url.query and not url.fragment)
    except ValueError:
        return None
    name_normal = name.lower().strip()
    if name_normal in {"cc0", "cc0 1.0", "cc0 1.0 universal"}:
        if safe_cc and url.path.rstrip("/") == "/publicdomain/zero/1.0":
            return "CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"
        return None
    if name_normal in {"public domain", "public domain mark", "public domain mark 1.0"}:
        marked = safe_cc and url.path.rstrip("/") == "/publicdomain/mark/1.0"
        explicitly_free = not raw_url and _meta(metadata, "Copyrighted", 20).lower() == "false"
        if marked or explicitly_free:
            return "Public domain", "https://creativecommons.org/publicdomain/mark/1.0/"
        return None
    match = re.fullmatch(r"CC BY(-SA)? (1\.0|2\.0|2\.5|3\.0|4\.0)", name, re.IGNORECASE)
    if match and safe_cc:
        kind = "by-sa" if match.group(1) else "by"
        version = match.group(2)
        if url.path.rstrip("/") == f"/licenses/{kind}/{version}":
            label = f"CC {'BY-SA' if kind == 'by-sa' else 'BY'} {version}"
            return label, f"https://creativecommons.org/licenses/{kind}/{version}/"
    return None


def validate_image(data: bytes, mime: str) -> tuple[str, int, int]:
    """Verify a still raster, then decode it with hard resource limits."""
    if not data or len(data) > MAX_IMAGE_BYTES or mime not in MIME_FORMAT:
        raise CommonsError("图片类型或体积不符合限制")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                fmt = picture.format
                width, height = picture.size
                if (fmt != MIME_FORMAT[mime] or getattr(picture, "n_frames", 1) != 1
                        or width < MIN_WIDTH or height < MIN_HEIGHT or width <= height
                        or width > 1920 or width > MAX_DIMENSION or height > MAX_DIMENSION
                        or width * height > MAX_PIXELS or width / height > 3.2):
                    raise CommonsError("只接收足够清晰的静态横图")
                picture.verify()
            with Image.open(io.BytesIO(data)) as picture:
                picture.load()  # Reject truncated image data before storing it.
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise CommonsError("下载内容未通过图片验证") from exc
    return FORMAT_EXT[fmt], width, height


class CommonsClient:
    """fetch() returns downloaded/cached records without any system calls.

    Refresh jobs are limited to one per theme in six hours, even with force=True.
    fetch_many may page small batches serially within its bounded refresh job.
    force is retained for the settings UI but cannot override the source limit.
    Failed refreshes use the same cooldown and leave existing cache intact.
    The cache has a hard quota; it does not remove somebody's favorite/current
    source file behind the application's back.
    """
    def __init__(self, cache_root: Path, rng: random.Random | None = None):
        self.root = Path(cache_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.images = self.root / "commons"
        self.images.mkdir(exist_ok=True)
        if not self.images.resolve().is_relative_to(self.root):
            raise CommonsError("缓存目录不能指向外部路径")
        self.index = self.root / "commons-index.json"
        if self.index.is_symlink():
            raise CommonsError("缓存索引不能是符号链接")
        self._lock = threading.RLock()
        self._opener = build_opener(_SafeRedirect())
        self._last_request = 0.0
        self._rng = rng if rng is not None else random.Random()
        self._job: dict | None = None
        self.last_error = ""
        self.last_status = "尚未下载在线图片"
        self.last_stats: dict = {}
        self._state = self._load_state()

    def _safe_file(self, filename: str) -> Path:
        if not isinstance(filename, str) or not re.fullmatch(r"[a-f0-9]{64}\.(jpg|png|webp)", filename):
            raise CommonsError("缓存文件名无效")
        target = self.images / filename
        if not target.resolve().is_relative_to(self.images.resolve()) or target.is_symlink():
            raise CommonsError("缓存文件不能指向外部路径")
        return target

    def _load_state(self) -> dict:
        empty = {"version": 1, "themes": {}, "records": []}
        try:
            if self.index.stat().st_size > MAX_JSON_BYTES:
                return empty
            state = json.loads(self.index.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("version") != 1:
                return empty
            if not isinstance(state.get("themes"), dict) or not isinstance(state.get("records"), list):
                return empty
            accepted = []
            for item in state["records"][:500]:
                if not isinstance(item, dict) or item.get("theme") not in THEMES:
                    continue
                try:
                    target = self._safe_file(item.get("filename", ""))
                    if (not target.is_file() or target.stat().st_size > MAX_IMAGE_BYTES
                            or item.get("id") != target.stem):
                        continue
                    # Rebuild paths. Never honor absolute paths in JSON.
                    record = {key: item.get(key, "") for key in PUBLIC_KEYS}
                    record.update(path=str(target.resolve()), origin="online", album="", tags=[])
                    record.update(filename=target.name, download_url=item.get("download_url", ""),
                                  source_sha1=item.get("source_sha1", ""))
                    accepted.append(record)
                except (OSError, CommonsError):
                    continue
            state["records"] = accepted
            state["themes"] = {k: v for k, v in state["themes"].items() if k in THEMES and isinstance(v, dict)}
            return state
        except (OSError, ValueError, TypeError):
            return empty

    def _save_state(self):
        if self.index.is_symlink():
            raise CommonsError("缓存索引不能是符号链接")
        temp = self.root / (".commons-index-" + uuid.uuid4().hex + ".tmp")
        try:
            temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.index)
        finally:
            temp.unlink(missing_ok=True)

    def _check_cancel(self):
        event = self._job.get("cancel_event") if self._job else None
        if event is not None and event.is_set():
            raise _Cancelled("已取消在线下载")

    def _wait(self, seconds: float):
        self._check_cancel()
        event = self._job.get("cancel_event") if self._job else None
        if event is not None:
            if event.wait(max(0, seconds)):
                raise _Cancelled("已取消在线下载")
        elif seconds > 0:
            time.sleep(seconds)
        self._check_cancel()

    def _report(self, message: str):
        self.last_status = message
        callback = self._job.get("progress") if self._job else None
        if callback is not None:
            try:
                callback(message)
            except Exception:
                # UI notification failures must not invalidate a committed file.
                pass

    def _retry(self, operation):
        for attempt in range(3):
            self._check_cancel()
            try:
                result = operation()
                self._check_cancel()
                return result
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise CommonsError(f"Commons 请求失败（HTTP {exc.code}）") from exc
                try:
                    delay = float(exc.headers.get("Retry-After", 0))
                except (ValueError, TypeError):
                    delay = 0
                failure = _TransientError("Commons 暂时繁忙", delay)
            except _TransientError as exc:
                failure = exc
            except (URLError, TimeoutError, ConnectionError, OSError) as exc:
                failure = _TransientError("网络连接失败")
            if attempt == 2 or failure.retry_after > 30:
                raise failure
            self._report(f"网络暂时繁忙，准备第 {attempt + 2} 次重试")
            self._wait(max(2 ** (attempt + 1), min(30, failure.retry_after)))
        raise CommonsError("Commons 请求失败")

    def _request_once(self, url: str, max_bytes: int, expected_mimes: set[str]) -> tuple[bytes, str]:
        self._check_cancel()
        if self._job and self._job["http_bytes"] >= MAX_JOB_BYTES:
            raise _BudgetExceeded("本次下载已达 120 MiB 流量上限")
        validate_download_url(url)
        gap = 1.0 - (time.monotonic() - self._last_request)
        if gap > 0:
            self._wait(gap)
        self._last_request = time.monotonic()
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": ", ".join(sorted(expected_mimes)),
                                        "Accept-Encoding": "identity"})
        with self._opener.open(request, timeout=15) as response:
            self._check_cancel()
            validate_download_url(response.geturl())
            mime = response.headers.get_content_type().lower()
            if mime not in expected_mimes:
                raise CommonsError("服务器返回了非预期的内容类型")
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if encoding not in {"identity", ""}:
                raise CommonsError("服务器返回了未请求的压缩内容")
            length = response.headers.get("Content-Length")
            declared_length = None
            if length is not None:
                try:
                    declared_length = int(length)
                    if declared_length < 0 or declared_length > max_bytes:
                        raise CommonsError("下载内容超过体积限制")
                except ValueError as exc:
                    raise CommonsError("服务器内容长度无效") from exc
            chunks, total = [], 0
            while True:
                self._check_cancel()
                read_size = min(64 * 1024, max_bytes + 1 - total)
                if self._job:
                    remaining = MAX_JOB_BYTES - self._job["http_bytes"]
                    if remaining <= 0:
                        if declared_length is not None and total == declared_length:
                            break
                        raise _BudgetExceeded("本次下载已达 120 MiB 流量上限")
                    read_size = min(read_size, remaining)
                chunk = response.read(read_size)
                if self._job:
                    self._job["http_bytes"] += len(chunk)
                self._check_cancel()
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise CommonsError("下载内容超过体积限制")
                chunks.append(chunk)
            self._check_cancel()
            return b"".join(chunks), mime

    def _api(self, params: dict) -> dict:
        query = {"action": "query", "format": "json", "formatversion": "2", "maxlag": "5", **params}
        url = API_URL + "?" + urlencode(query)

        def operation():
            body, _ = self._request_once(url, MAX_JSON_BYTES, {"application/json"})
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError) as exc:
                raise CommonsError("Commons 返回了无效 JSON") from exc
            if not isinstance(payload, dict):
                raise CommonsError("Commons 数据格式无效")
            if "error" in payload:
                error = payload["error"]
                if isinstance(error, dict) and error.get("code") in {"maxlag", "ratelimited", "readonly"}:
                    raise _TransientError("Commons 暂时繁忙", 5)
                raise CommonsError("Commons API 拒绝了请求")
            return payload

        return self._retry(operation)

    def _candidate(self, page: dict, theme: str) -> dict | None:
        try:
            info = page["imageinfo"][0]
            if not isinstance(info, dict):
                return None
            metadata = info.get("extmetadata", {})
            license_info = verified_license(metadata)
            author = _meta(metadata, "Artist", 600)
            if license_info is None or not author:
                return None
            if info.get("mime") not in MIME_FORMAT or info.get("mediatype", "BITMAP") != "BITMAP":
                return None
            width, height = int(info["width"]), int(info["height"])
            if width < MIN_WIDTH or height < MIN_HEIGHT or width <= height or width / height > 3.2:
                return None
            source = validate_download_url(info["descriptionurl"])
            source_parts = urlsplit(source)
            if source_parts.hostname != "commons.wikimedia.org" or not source_parts.path.startswith("/wiki/File:"):
                return None
            download = validate_download_url(info.get("thumburl") or info["url"])
            if urlsplit(download).hostname not in IMAGE_HOSTS:
                return None
            if not info.get("thumburl") and int(info.get("size", MAX_IMAGE_BYTES + 1)) > MAX_IMAGE_BYTES:
                return None
            if (info.get("thumburl") and int(info.get("thumbwidth", 1920)) > 1920
                    or not info.get("thumburl") and width > 1920):
                return None
            title = clean_text(page.get("title", ""), 250)
            if not title.startswith("File:"):
                return None
            return {"title": title.removeprefix("File:"), "theme": theme, "author": author,
                    "source_url": source, "license_name": license_info[0], "license_url": license_info[1],
                    "download_url": download,
                    "source_sha1": info.get("sha1", "") if isinstance(info.get("sha1", ""), str)
                    and re.fullmatch(r"[a-z0-9]{30,40}", info.get("sha1", "")) else "",
                    "crop_note": "Commons 缩略图仅缩放、未裁切；填充桌面时可能由应用居中裁切。"}
        except (KeyError, TypeError, ValueError, IndexError, CommonsError):
            return None

    def _cache_size(self) -> int:
        # No recursive traversal: only our chosen cache directory is inspected.
        return sum(p.stat().st_size for p in self.images.iterdir() if p.is_file() and not p.is_symlink())

    def _download(self, candidate: dict) -> dict:
        self._check_cancel()
        existing = self._matching_source(candidate)
        if existing:
            # One content file can belong to two selected themes. Preserve the
            # second theme record without downloading the shared source twice.
            same_theme = next((r for r in self._state["records"]
                               if r["id"] == existing["id"] and r["theme"] == candidate["theme"]), None)
            if same_theme:
                return same_theme
            themed = {**existing, "theme": candidate["theme"]}
            self._state["records"].append(themed)
            try:
                self._save_state()
            except (OSError, CommonsError):
                self._state["records"].remove(themed)
                raise
            return themed
        data, mime = self._retry(lambda: self._request_once(candidate["download_url"], MAX_IMAGE_BYTES, set(MIME_FORMAT)))
        extension, _, _ = validate_image(data, mime)
        self._check_cancel()
        digest = hashlib.sha256(data).hexdigest()
        target = self._safe_file(digest + extension)
        if not target.exists():
            if self._cache_size() + len(data) > MAX_CACHE_BYTES:
                raise _CacheFull("在线缓存已达 200 MB，暂不继续下载；现有图片仍可使用")
            temp = self.images / (".download-" + uuid.uuid4().hex + ".tmp")
            try:
                temp.write_bytes(data)
                self._check_cancel()
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        record = {**candidate, "id": digest, "path": str(target.resolve()), "filename": target.name,
                  "origin": "online", "album": "", "tags": []}
        duplicate = next((r for r in self._state["records"] if r["id"] == digest and r["theme"] == record["theme"]), None)
        if duplicate:
            return duplicate
        self._state["records"].append(record)
        # Commit after each image, before reporting progress. Cancellation then
        # returns only records that survive application restart.
        try:
            self._save_state()
        except (OSError, CommonsError):
            self._state["records"].remove(record)
            raise
        return record

    def _cached(self, theme: str, limit: int) -> list[dict]:
        result = []
        for record in reversed(self._state["records"]):
            try:
                if record["theme"] == theme and self._safe_file(record["filename"]).is_file():
                    result.append(self._public(record))
                    if len(result) >= limit:
                        break
            except (CommonsError, OSError):
                continue
        return result

    def _public(self, record: dict) -> dict:
        result = {key: record.get(key, "") for key in PUBLIC_KEYS}
        result["tags"] = list(result["tags"]) if isinstance(result["tags"], list) else []
        result["themes"] = list(dict.fromkeys(r["theme"] for r in self._state["records"]
                                             if r["id"] == record["id"]))
        return result

    def _matching_source(self, candidate: dict) -> dict | None:
        for record in self._state["records"]:
            same_source = record.get("source_url") == candidate["source_url"]
            same_sha1 = candidate.get("source_sha1") and record.get("source_sha1") == candidate["source_sha1"]
            if same_source or same_sha1:
                try:
                    if self._safe_file(record["filename"]).is_file():
                        return record
                except (CommonsError, OSError):
                    continue
        return None

    def _known_source(self, candidate: dict) -> bool:
        return self._matching_source(candidate) is not None

    def _many_cached(self, themes: list[str], limit: int, selected: list[dict]) -> list[dict]:
        result = [self._public(record) for record in selected]
        seen = {record["id"] for record in result}
        pools = {theme: self._cached(theme, MAX_JOB_IMAGES) for theme in themes}
        for pool in pools.values():
            self._rng.shuffle(pool)
        order = list(themes)
        self._rng.shuffle(order)
        while len(result) < limit and any(pools.values()):
            for theme in order:
                while pools[theme]:
                    record = pools[theme].pop()
                    if record["id"] not in seen:
                        result.append(self._public(record))
                        seen.add(record["id"])
                        self.last_stats["themes"][theme]["cached"] += 1
                        break
                if len(result) >= limit:
                    break
        return result

    def _advance_many_batch(self, theme: str, run: dict):
        if run["pool"] or run["batch"] is None:
            return
        index, token = run["batch"]
        state = run["state"]
        category = THEMES[theme][index]
        if token is not None:
            state["cursors"][category] = token
            state["cursor"] = token
            state["category_index"] = index
            run["categories"].append(index)
        else:
            state["cursors"].pop(category, None)
            state.pop("cursor", None)
            state["category_index"] = (index + 1) % len(THEMES[theme])
        run["batch"] = None
        self._save_state()

    def fetch_many(self, themes: list[str], limit: int = 10,
                   cancel_event: threading.Event | None = None,
                   progress: Callable[[str], None] | None = None) -> list[dict]:
        """Refresh several themes in a bounded, cancellable serial job.

        ``progress`` receives a Chinese string. ``last_stats`` has cancelled,
        downloaded, returned, candidates, http_bytes, limited and themes keys.
        Each theme allows a new refresh job every six hours. Within that job,
        small batches are paged serially; requests plus failed retries count
        toward the 120 MiB budget. Cancellation can take up to one socket
        timeout (15s), and returns this job's durably committed new/reused records.
        Normal completion may append existing cache to reach the requested
        total. The caller's library/favorite records are never edited here.
        Pass random.Random(seed) as constructor rng for reproducible testing.
        """
        if not isinstance(themes, (list, tuple)) or not themes:
            raise ValueError("请至少选择一个在线主题")
        chosen = list(dict.fromkeys(themes))
        if any(theme not in THEMES for theme in chosen):
            raise ValueError("未知在线主题")
        limit = max(1, min(MAX_JOB_IMAGES, int(limit)))
        with self._lock:
            self.last_error = ""
            stats = {"cancelled": False, "downloaded": 0, "returned": 0,
                     "candidates": 0, "requested_candidates": 0, "http_bytes": 0, "limited": False,
                     "themes": {theme: {"downloaded": 0, "cached": 0, "filtered": 0,
                                        "duplicate": 0, "failed": 0, "batches": 0,
                                        "status": "尚未查询", "error": ""} for theme in chosen}}
            self.last_stats = stats
            self._job = {"cancel_event": cancel_event, "progress": progress, "http_bytes": 0}
            committed: list[dict] = []
            ready: list[dict] = []
            ready_ids: set[str] = set()
            new_ids: set[str] = set()
            previous_ids = {record["id"] for record in self._state["records"]}
            runs: dict[str, dict] = {}
            try:
                self._check_cancel()
                if self._cache_size() >= MAX_CACHE_BYTES:
                    raise _CacheFull("在线缓存已达 200 MB，暂不继续下载；现有图片仍可使用")
                now = time.time()
                for theme in chosen:
                    state = self._state["themes"].setdefault(theme, {})
                    try:
                        last_attempt = float(state.get("last_attempt", 0))
                    except (TypeError, ValueError):
                        last_attempt = 0
                    if now < last_attempt + QUERY_INTERVAL:
                        stats["themes"][theme]["status"] = "6 小时冷却中，使用已有缓存"
                        continue
                    categories = list(range(len(THEMES[theme])))
                    self._rng.shuffle(categories)
                    cursors = state.get("cursors")
                    if not isinstance(cursors, dict):
                        cursors = {}
                        # Migrate the previous single-theme cursor without
                        # querying or changing existing image records.
                        cursor = state.get("cursor")
                        old_index = state.get("category_index", 0)
                        if isinstance(cursor, str) and isinstance(old_index, int):
                            cursors[THEMES[theme][old_index % len(THEMES[theme])]] = cursor
                        state["cursors"] = cursors
                    runs[theme] = {"state": state, "categories": categories,
                                   "pool": [], "started": False, "batch": None}
                order = list(runs)
                self._rng.shuffle(order)
                self._report(f"准备获取 {limit} 张：已选择 {len(chosen)} 个主题")
                while order and len(ready) < limit:
                    self._check_cancel()
                    progressed = False
                    for theme in list(order):
                        self._check_cancel()
                        if len(ready) >= limit:
                            break
                        run = runs[theme]
                        state = run["state"]
                        theme_stats = stats["themes"][theme]
                        # Finish a cursor only when its candidate pool has been
                        # consumed. Full/cancelled jobs leave it at the batch's
                        # start so the unused metadata can be retried next time.
                        if not run["pool"] and run["batch"] is not None:
                            self._advance_many_batch(theme, run)
                        if not run["pool"]:
                            if not run["categories"]:
                                theme_stats["status"] = "候选已用尽"
                                order.remove(theme)
                                continue
                            if stats["requested_candidates"] >= MAX_JOB_CANDIDATES:
                                stats["limited"] = True
                                order.remove(theme)
                                continue
                            if not run["started"]:
                                state["last_attempt"] = time.time()
                                self._save_state()  # Gate persists before I/O.
                                run["started"] = True
                            index = run["categories"].pop(0)
                            category = THEMES[theme][index]
                            count = min(12, MAX_JOB_CANDIDATES - stats["requested_candidates"])
                            # Charge requested slots, even if a broken API sends
                            # an empty page with a continuation forever.
                            stats["requested_candidates"] += count
                            params = {"generator": "categorymembers", "gcmtitle": category,
                                      "gcmtype": "file", "gcmlimit": str(count), "prop": "imageinfo",
                                      "iiprop": "url|size|mime|mediatype|sha1|extmetadata", "iiurlwidth": "1920",
                                      "iiextmetadatalanguage": "en",
                                      "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|Copyrighted"}
                            cursor = state["cursors"].get(category)
                            if isinstance(cursor, str) and len(cursor) < 1000 and not any(ord(c) < 32 for c in cursor):
                                params["gcmcontinue"] = cursor
                            theme_stats["batches"] += 1
                            self._report(f"正在筛选{THEME_LABELS[theme]}，已保存 {len(committed)}/{limit} 张")
                            try:
                                payload = self._api(params)
                                self._check_cancel()
                                query = payload.get("query", {})
                                pages = query.get("pages", []) if isinstance(query, dict) else None
                                if not isinstance(pages, list):
                                    raise CommonsError("Commons 图片列表格式无效")
                                pages = pages[:count]
                                stats["candidates"] += len(pages)
                                continuation = payload.get("continue", {})
                                token = continuation.get("gcmcontinue") if isinstance(continuation, dict) else None
                                if not isinstance(token, str) or len(token) >= 1000 or any(ord(c) < 32 for c in token):
                                    token = None
                                run["batch"] = (index, token)
                                for page in pages:
                                    candidate = self._candidate(page, theme) if isinstance(page, dict) else None
                                    if candidate is None:
                                        theme_stats["filtered"] += 1
                                    else:
                                        run["pool"].append(candidate)
                                self._rng.shuffle(run["pool"])
                            except (_Cancelled, _BudgetExceeded, _CacheFull):
                                raise
                            except (CommonsError, OSError, TypeError, ValueError) as exc:
                                theme_stats["failed"] += 1
                                theme_stats["error"] = str(exc)
                                theme_stats["status"] = "请求失败，保留缓存"
                                self.last_error = str(exc)
                                order.remove(theme)
                                continue
                            if not run["pool"]:
                                progressed = True
                                continue
                        candidate = run["pool"].pop()
                        try:
                            record = self._download(candidate)
                            if record["id"] not in previous_ids and record["id"] not in new_ids:
                                new_ids.add(record["id"])
                                committed.append(record)
                                stats["downloaded"] = len(committed)
                                theme_stats["downloaded"] += 1
                                theme_stats["status"] = "已有新图"
                                self._report(f"已保存 {len(committed)}/{limit} 张 · {THEME_LABELS[theme]}")
                            else:
                                theme_stats["duplicate"] += 1
                                theme_stats["cached"] += 1
                                theme_stats["status"] = "已复用缓存并保留主题关联"
                            if record["id"] not in ready_ids:
                                ready.append(record)
                                ready_ids.add(record["id"])
                            self._advance_many_batch(theme, run)
                        except (_Cancelled, _BudgetExceeded, _CacheFull):
                            raise
                        except (CommonsError, OSError) as exc:
                            self.last_error = str(exc)
                            theme_stats["failed"] += 1
                            theme_stats["error"] = str(exc)
                        progressed = True
                    if not progressed:
                        break
            except _Cancelled:
                stats["cancelled"] = True
            except (_BudgetExceeded, _CacheFull) as exc:
                stats["limited"] = True
                self.last_error = str(exc)
            except (CommonsError, OSError) as exc:
                self.last_error = str(exc)
            finally:
                stats["http_bytes"] = self._job["http_bytes"]
                if stats["cancelled"]:
                    result = [self._public(record) for record in ready]
                    self._report(f"已取消，已保存 {len(committed)} 张新图，复用 {len(ready) - len(committed)} 张缓存")
                else:
                    result = self._many_cached(chosen, limit, ready)
                    cached = len(result) - len(committed)
                    suffix = ""
                    if stats["limited"]:
                        suffix = f"；达到限额（{self.last_error or '240 个候选'}）"
                    elif self.last_error:
                        suffix = f"；部分请求失败：{self.last_error}"
                    elif not committed and any(r["started"] for r in runs.values()):
                        suffix = "；没有新的候选通过横图尺寸、明确许可或不重复筛选"
                    elif not runs:
                        suffix = "；所选主题处于 6 小时冷却期"
                    self._report(f"本次保存 {len(committed)} 张新图，可用缓存 {cached} 张{suffix}")
                stats["returned"] = len(result)
                stats["cached"] = max(0, len(result) - len(committed))
                self._job = None
            return result

    def fetch(self, theme: str, limit: int = 4, force: bool = False) -> list[dict]:
        if theme not in THEMES:
            raise ValueError("未知在线主题")
        limit = max(1, min(4, int(limit)))
        with self._lock:
            self.last_error = ""
            state = self._state["themes"].setdefault(theme, {})
            now = time.time()
            try:
                last_attempt = float(state.get("last_attempt", 0))
            except (TypeError, ValueError):
                last_attempt = 0
            # A future timestamp is conservatively kept as a cooldown.
            if now < last_attempt + QUERY_INTERVAL:
                self.last_status = "使用在线缓存（每个主题至少间隔 6 小时刷新）"
                return self._cached(theme, limit)
            state["last_attempt"] = now
            try:
                self._save_state()  # Persist the gate before network I/O.
                try:
                    category_index = int(state.get("category_index", 0)) % len(THEMES[theme])
                except (TypeError, ValueError):
                    category_index = 0
                params = {"generator": "categorymembers", "gcmtitle": THEMES[theme][category_index],
                          "gcmtype": "file", "gcmlimit": "12", "prop": "imageinfo",
                          "iiprop": "url|size|mime|mediatype|sha1|extmetadata", "iiurlwidth": "1920",
                          "iiextmetadatalanguage": "en",
                          "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|Copyrighted"}
                cursor = state.get("cursor")
                if isinstance(cursor, str) and len(cursor) < 1000 and not any(ord(c) < 32 for c in cursor):
                    params["gcmcontinue"] = cursor
                payload = self._api(params)
                query = payload.get("query", {})
                if not isinstance(query, dict):
                    raise CommonsError("Commons 图片列表格式无效")
                pages = query.get("pages", [])
                if not isinstance(pages, list):
                    raise CommonsError("Commons 图片列表格式无效")
                found = 0
                for page in pages[:12]:
                    if not isinstance(page, dict):
                        continue
                    candidate = self._candidate(page, theme)
                    if candidate is None:
                        continue
                    try:
                        self._download(candidate)
                        found += 1
                    except (CommonsError, OSError) as exc:
                        self.last_error = str(exc)
                    if found >= limit:
                        break
                continuation = payload.get("continue", {})
                token = continuation.get("gcmcontinue") if isinstance(continuation, dict) else None
                if isinstance(token, str) and len(token) < 1000:
                    state["cursor"] = token
                else:
                    state.pop("cursor", None)
                    state["category_index"] = (category_index + 1) % len(THEMES[theme])
                if found:
                    self.last_status = f"在线刷新完成，本次可用 {found} 张（只接收许可明确的横图）"
                elif self.last_error:
                    self.last_status = f"本次未下载新图：{self.last_error}；保留现有缓存"
                else:
                    self.last_status = "本批精选候选均未通过横图尺寸或明确许可筛选；保留缓存，至少 6 小时后可取下一批"
                self._save_state()
            except (CommonsError, OSError, TypeError, ValueError) as exc:
                self.last_error = str(exc)
                self.last_status = "在线刷新失败，保留现有缓存和当前壁纸"
            return self._cached(theme, limit)
