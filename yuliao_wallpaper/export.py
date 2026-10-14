"""Explicit, bounded image exports with attribution and no source-path leaks."""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import stat
import unicodedata
import uuid
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from .store import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, MIN_IMAGE_SIDE, _is_link_or_reparse


MAX_EXPORT_RECORDS = 500
MAX_EXPORT_BYTES = 500 * 1024 * 1024
FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp"}
FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "BMP": "image/bmp"}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _cancelled(event) -> bool:
    return event is not None and event.is_set()


def _text(value, limit: int = 2000) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(char for char in value if ord(char) >= 32 and ord(char) != 127).split())[:limit]


def _url(value) -> str:
    value = _text(value, 16384)
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return ""


def _safe_stem(title: str) -> str:
    title = unicodedata.normalize("NFKC", _text(title, 300))
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    title = os.path.splitext(title)[0].strip(" .")[:80].rstrip(" .") or "图片"
    if title.split(".", 1)[0].upper() in _RESERVED_NAMES:
        title = "图片_" + title
    return title


def _check_components(path: Path) -> None:
    for component in reversed([path, *path.parents]):
        try:
            if _is_link_or_reparse(component):
                raise ValueError("不通过符号链接或连接点导出")
        except FileNotFoundError:
            continue


def _destination(path: Path) -> Path:
    destination = Path(os.path.abspath(Path(path).expanduser()))
    _check_components(destination)
    destination.mkdir(parents=True, exist_ok=True)
    _check_components(destination)
    if not destination.is_dir():
        raise ValueError("请选择导出目录")
    return destination


def _read_image(record: dict) -> tuple[bytes, str, str, int, int]:
    supplied = record.get("path")
    if not isinstance(supplied, str) or not Path(supplied).is_absolute():
        raise ValueError("图片路径无效")
    path = Path(supplied)
    _check_components(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("图片不是普通文件")
    if info.st_size > MAX_IMAGE_BYTES:
        raise ValueError("单张图片超过 30 MiB")
    with path.open("rb") as source:
        content = source.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("单张图片超过 30 MiB")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as picture:
            image_format = picture.format
            width, height = picture.size
            if image_format not in FORMAT_EXTENSIONS:
                raise ValueError("图片实际格式不支持导出")
            if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("图片尺寸不符合导出限制（至少 64 像素，最多 4000 万像素）")
            picture.verify()
        with Image.open(io.BytesIO(content)) as picture:
            picture.load()
    return content, FORMAT_EXTENSIONS[image_format], FORMAT_MIME[image_format], width, height


def _write_exclusive(path: Path, content: bytes, event=None) -> bool:
    """Create a new file only; remove our incomplete file on cancellation/error."""
    created = False
    try:
        with path.open("xb") as output:
            created = True
            for offset in range(0, len(content), 1024 * 1024):
                if _cancelled(event):
                    break
                output.write(content[offset:offset + 1024 * 1024])
            else:
                output.flush()
                os.fsync(output.fileno())
                return True
        if created:
            path.unlink(missing_ok=True)
        return False
    except OSError:
        if created:
            path.unlink(missing_ok=True)
        raise


def _markdown(records: list[dict], batch_id: str) -> bytes:
    def escape(value) -> str:
        return re.sub(r"([\\`*_{}\[\]#|])", r"\\\1", html.escape(_text(value, 16384)))

    lines = ["# 雨聊壁纸导出清单", "", f"批次：{batch_id}", "",
             "此清单仅对应本次明确选中的图片。图片为原始文件的副本；应用未改变原图。",
             "本地图片的使用权由使用者自行确认。在线图片请按对应许可证保留署名与来源。",
             "裁切说明描述桌面显示方式；导出的图片没有进行桌面裁切。", ""]
    for index, record in enumerate(records, 1):
        lines.extend([
            f"## {index}. {escape(record['title'])}", "",
            f"- 导出文件：{escape(record['filename'])}",
            f"- 作者：{escape(record['author']) or '未提供'}",
            f"- 许可：{escape(record['license_name']) or '请自行确认使用权'}",
            f"- 许可链接：{escape(record['license_url']) or '未提供'}",
            f"- 来源页：{escape(record['source_url']) or '本地导入，未提供公开来源页'}",
            f"- 裁切说明：{escape(record['crop_note']) or '导出原图，不进行裁切'}",
            f"- SHA-256：{record['sha256']}", "",
        ])
    return "\n".join(lines).encode("utf-8")


def export_records(records: list[dict], destination: Path, cancel_event=None) -> dict:
    """Copy only this explicit selection, with bounded decoding and attribution.

    Return filenames, never source absolute paths. Existing destination files
    are never overwritten. A cancelled batch keeps complete copies and, when
    any were written, a matching manifest; partial image files are removed.
    """
    result = {"count": 0, "errors": [], "files": [], "manifest": "",
              "manifest_markdown": "", "cancelled": False, "total_bytes": 0}
    if _cancelled(cancel_event):
        result["cancelled"] = True
        return result
    if not isinstance(records, list):
        raise TypeError("records 必须是明确选中的图片列表")
    if not records:
        return result
    try:
        directory = _destination(destination)
    except (OSError, ValueError) as error:
        result["errors"].append(f"导出目录不可用：{error}")
        return result
    if len(records) > MAX_EXPORT_RECORDS:
        result["errors"].append(f"本次最多导出 {MAX_EXPORT_RECORDS} 张图片，超出的选择未处理")
    exported = []
    for record in records[:MAX_EXPORT_RECORDS]:
        if _cancelled(cancel_event):
            result["cancelled"] = True
            break
        title = _text(record.get("title"), 1000) if isinstance(record, dict) else "图片"
        try:
            if not isinstance(record, dict):
                raise ValueError("图片记录无效")
            content, extension, mime, width, height = _read_image(record)
            if result["total_bytes"] + len(content) > MAX_EXPORT_BYTES:
                raise ValueError("本次图片合计超过 500 MiB，此图片未导出")
            if _cancelled(cancel_event):
                result["cancelled"] = True
                break
            digest = hashlib.sha256(content).hexdigest()
            stem = f"{_safe_stem(title)}-{digest[:12]}"
            copied = False
            for counter in range(1, 10001):
                filename = f"{stem}{'-' + str(counter) if counter > 1 else ''}{extension}"
                target = directory / filename
                if target.parent != directory:
                    raise ValueError("导出文件名无效")
                try:
                    copied = _write_exclusive(target, content, cancel_event)
                    break
                except FileExistsError:
                    continue
            else:
                raise ValueError("同名导出文件过多，请选择另一个目录")
            if not copied:
                result["cancelled"] = True
                break
            exported.append({
                "filename": filename, "sha256": digest, "title": title,
                "origin": "local" if record.get("origin") == "local" else "online",
                "author": _text(record.get("author")),
                "source_url": _url(record.get("source_url")),
                "license_name": _text(record.get("license_name")),
                "license_url": _url(record.get("license_url")),
                "crop_note": _text(record.get("crop_note")),
                "mime": mime, "width": width, "height": height, "bytes": len(content),
            })
            result["files"].append(filename)
            result["count"] += 1
            result["total_bytes"] += len(content)
        except (OSError, ValueError, TypeError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            result["errors"].append(f"{title or '图片'}：{error}")
    if exported:
        batch_id = uuid.uuid4().hex
        manifest_name = f"雨聊壁纸-导出清单-{batch_id}.json"
        markdown_name = f"雨聊壁纸-导出清单-{batch_id}.md"
        manifest = {"version": 1, "batch_id": batch_id, "count": len(exported),
                    "total_bytes": result["total_bytes"], "cancelled": result["cancelled"], "records": exported}
        for name, content, key in (
            (manifest_name, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"), "manifest"),
            (markdown_name, _markdown(exported, batch_id), "manifest_markdown"),
        ):
            try:
                # Finish attribution for copies already completed, even after
                # a cancellation; no additional image copy occurs here.
                if _write_exclusive(directory / name, content):
                    result[key] = name
            except OSError as error:
                result["errors"].append(f"导出清单写入失败：{error}")
    return result
