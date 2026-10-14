"""Private local state and bounded, read-only image imports.

The application supplies a private user-data directory. Imported files remain in
their original location; this module never uploads, renames, or edits them.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError


SUPPORTED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})
SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "BMP"})
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MIN_IMAGE_SIDE = 64
MAX_FOLDER_FILES = 1000
MAX_CONFIG_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_HISTORY = 2000
IMAGE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_SETTINGS: dict[str, Any] = {
    "mode": "mixed",
    "theme": "landscape",
    "themes": ["landscape"],
    "interval_seconds": 1800,
    "download_hours": 6,
    "download_count": 10,
    "album": "",
    "tag": "",
    "favorites_only": False,
}
DEFAULT_STATE: dict[str, Any] = {
    "history": [],
    "history_index": -1,
    "seen": [],
    "current_id": "",
    "order": [],
    "removed_ids": [],
}


def _is_link_or_reparse(path: Path) -> bool:
    """Reject symlinks and Windows junctions before following them."""
    details = path.lstat()
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _clean_labels(values: Iterable[Any] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()[:80]
        if value and value not in result:
            result.append(value)
        if len(result) >= 30:
            break
    return result


class Store:
    """State owned by this application, saved with atomic replacement."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root).expanduser().absolute()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_root / "config.json"
        self.settings = {**DEFAULT_SETTINGS, "themes": list(DEFAULT_SETTINGS["themes"])}
        self.records: dict[str, dict[str, Any]] = {}
        self.state: dict[str, Any] = {
            **DEFAULT_STATE,
            "history": [],
            "seen": [],
            "order": [],
            "removed_ids": [],
        }
        self.warnings: list[str] = []
        self._needs_migration_backup = False
        self._load()

    @property
    def status(self) -> str:
        return "；".join(self.warnings)

    @property
    def removed_records(self) -> dict[str, dict[str, Any]]:
        return {key: record for key, record in self.records.items() if record.get("deleted")}

    def ordered_records(self, include_removed: bool = False) -> list[dict[str, Any]]:
        self._normalize_state()
        return [self.records[key] for key in self.state["order"]
                if include_removed or not self.records[key].get("deleted")]

    def _load(self) -> None:
        if not self.config_path.exists():
            return
        try:
            with self.config_path.open("rb") as handle:
                payload = handle.read(MAX_CONFIG_BYTES + 1)
            if len(payload) > MAX_CONFIG_BYTES:
                raise ValueError("配置文件过大")
            document = json.loads(payload.decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("配置应为对象")
            settings = document.get("settings", {})
            records = document.get("records", {})
            state_data = document.get("state", {})
            if not all(isinstance(value, dict) for value in (settings, records, state_data)):
                raise ValueError("配置结构不正确")
            if len(records) > MAX_RECORDS:
                raise ValueError("图片记录过多")
            loaded_records: dict[str, dict[str, Any]] = {}
            for record_id, record in records.items():
                if not isinstance(record_id, str) or not isinstance(record, dict):
                    raise ValueError("图片记录不正确")
                if record.get("id", record_id) != record_id:
                    raise ValueError("图片标识不一致")
                self._validate_record({**record, "id": record_id})
                loaded_records[record_id] = {
                    **record,
                    "id": record_id,
                    "favorite": record.get("favorite") is True,
                    "blocked": record.get("blocked") is True,
                    "tags": _clean_labels(record.get("tags", [])),
                }
            for key in ("history", "seen"):
                if not isinstance(state_data.get(key, []), list):
                    raise ValueError("历史记录不正确")
            self.settings.update(settings)
            if "themes" not in settings:
                old_theme = settings.get("theme", "landscape")
                self.settings["themes"] = (["landscape", "city", "animals"]
                                           if old_theme in {"", "all"} else [old_theme])
            self._normalize_settings()
            self.records = loaded_records
            self.state.update(state_data)
            # Remap the cursor against original positions before dropping
            # damaged/missing entries; prefiltering would shift its meaning.
            self._normalize_state()
            self._needs_migration_backup = document.get("version") != 2
        except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as error:
            self.settings = {**DEFAULT_SETTINGS, "themes": list(DEFAULT_SETTINGS["themes"])}
            self.records = {}
            self.state = {**DEFAULT_STATE, "history": [], "seen": [], "order": [], "removed_ids": []}
            self._preserve_corrupt_config(error)

    def _normalize_settings(self) -> None:
        if self.settings.get("mode") not in {"local", "online", "mixed"}:
            self.settings["mode"] = DEFAULT_SETTINGS["mode"]
        for key in ("theme", "album", "tag"):
            value = self.settings.get(key)
            self.settings[key] = value[:200] if isinstance(value, str) else DEFAULT_SETTINGS[key]
        selected = self.settings.get("themes")
        self.settings["themes"] = (_clean_labels(selected) if isinstance(selected, list)
                                   else [self.settings["theme"] or "landscape"])
        interval = self.settings.get("interval_seconds")
        if isinstance(interval, bool) or not isinstance(interval, int) or not 0 <= interval <= 31_536_000:
            self.settings["interval_seconds"] = DEFAULT_SETTINGS["interval_seconds"]
        elif 0 < interval < 300:
            self.settings["interval_seconds"] = 300
        download = self.settings.get("download_hours")
        if (
            isinstance(download, bool) or not isinstance(download, (int, float))
            or not math.isfinite(download) or not 0 <= download <= 720
        ):
            self.settings["download_hours"] = DEFAULT_SETTINGS["download_hours"]
        count = self.settings.get("download_count")
        if isinstance(count, bool) or not isinstance(count, int):
            count = DEFAULT_SETTINGS["download_count"]
        self.settings["download_count"] = max(1, min(30, count))
        self.settings["favorites_only"] = self.settings.get("favorites_only") is True

    def _normalize_state(self) -> None:
        def ids(value: Any, require_record: bool = True) -> list[str]:
            if not isinstance(value, list):
                return []
            return list(dict.fromkeys(item for item in value
                                      if isinstance(item, str) and IMAGE_ID_PATTERN.fullmatch(item)
                                      and (not require_record or item in self.records)))
        order = ids(self.state.get("order", []))
        known_order = set(order)
        order.extend(key for key in self.records if key not in known_order)
        self.state["order"] = order
        removed = ids(self.state.get("removed_ids", []), require_record=False)
        removed.extend(key for key, record in self.records.items()
                       if record.get("deleted") is True and key not in removed)
        self.state["removed_ids"] = removed[-MAX_RECORDS:]
        removed_set = set(removed)
        for key, record in self.records.items():
            record["deleted"] = key in removed_set
        original = self.state.get("history", [])
        original = original if isinstance(original, list) else []
        cursor = self.state.get("history_index", -1)
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            cursor = -1
        valid_positions = [index for index, key in enumerate(original)
                           if isinstance(key, str) and key in self.records and key not in removed_set]
        retained = valid_positions[-MAX_HISTORY:]
        self.state["history"] = [original[index] for index in retained]
        self.state["history_index"] = sum(index <= cursor for index in retained) - 1
        self.state["seen"] = [key for key in ids(self.state.get("seen", []))
                              if key not in removed_set][-MAX_RECORDS:]
        current = self.state.get("current_id", "")
        self.state["current_id"] = current if isinstance(current, str) and current in self.records else ""

    @staticmethod
    def _validate_record(record: dict[str, Any]) -> None:
        record_id = record.get("id")
        if not isinstance(record_id, str) or not IMAGE_ID_PATTERN.fullmatch(record_id):
            raise ValueError("图片标识应为 SHA-256 摘要")
        path = record.get("path")
        title = record.get("title")
        if not isinstance(path, str) or not path or not Path(path).is_absolute():
            raise ValueError("图片路径不正确")
        if not isinstance(title, str) or not title or len(title) > 1000:
            raise ValueError("图片标题不正确")
        if record.get("origin") not in {"local", "online"}:
            raise ValueError("图片来源不正确")
        for key in ("theme", "album", "author", "source_url", "license_name", "license_url", "crop_note"):
            if key in record and not isinstance(record[key], str):
                raise ValueError(f"图片字段 {key} 不正确")
        if not isinstance(record.get("tags", []), list):
            raise ValueError("图片标签不正确")
        if (not isinstance(record.get("themes", []), list)
                or any(not isinstance(theme, str) for theme in record.get("themes", []))):
            raise ValueError("图片主题列表不正确")

    def _preserve_corrupt_config(self, error: Exception) -> None:
        # A unique name prevents a later recovery from destroying older evidence.
        backup = self.data_root / f"config.corrupt-{time.time_ns()}.json"
        try:
            with self.config_path.open("rb") as source, backup.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except OSError:
            # Refuse future writes if the original cannot be safely preserved.
            self._recovery_blocked = True
            self.warnings.append("配置无法读取且无法备份，请检查数据目录权限后重新启动；原文件未覆盖")
            return
        self.warnings.append(f"配置无法读取，已保留为 {backup.name} 并使用默认设置")

    def save(self) -> None:
        if getattr(self, "_recovery_blocked", False):
            raise OSError("损坏配置未能备份，拒绝覆盖原文件")
        self._normalize_settings()
        self._normalize_state()
        document = {
            "version": 2,
            "settings": self.settings,
            "records": self.records,
            "state": self.state,
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if len(payload) > MAX_CONFIG_BYTES:
            raise ValueError("配置记录过多，请减少图片记录后重试")
        if self._needs_migration_backup:
            self._backup_pre02()
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="config-", suffix=".tmp", dir=self.data_root, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_path)
            temporary = None
            # POSIX can persist the directory entry. Windows replacement is
            # atomic and the file contents were flushed before replacement.
            if os.name != "nt":
                directory_fd = os.open(self.data_root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _backup_pre02(self) -> None:
        """Preserve the exact old profile before the first v0.2 replacement."""
        backup = self.data_root / f"config.pre-0.2-{time.time_ns()}.json"
        created = False
        try:
            with self.config_path.open("rb") as source, backup.open("xb") as target:
                created = True
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except OSError:
            if created:
                backup.unlink(missing_ok=True)
            raise OSError("旧配置备份失败，未覆盖原配置；请检查数据目录权限")
        self._needs_migration_backup = False

    def add_records(self, records: list[dict[str, Any]], restore_removed: bool = False) -> int:
        """Upsert metadata while retaining choices made by the user."""
        additions = 0
        for incoming in records:
            if not isinstance(incoming, dict):
                raise ValueError("图片记录不正确")
            record_id = incoming.get("id")
            if not isinstance(record_id, str) or not IMAGE_ID_PATTERN.fullmatch(record_id):
                raise ValueError("图片记录缺少标识")
            existing = self.records.get(record_id)
            removed = record_id in self.state.get("removed_ids", []) or bool((existing or {}).get("deleted"))
            if removed and not restore_removed:
                continue
            updated = {**(existing or {}), **incoming}
            self._validate_record(updated)
            if existing is None:
                if len(self.records) >= MAX_RECORDS:
                    raise ValueError(f"最多保留 {MAX_RECORDS} 条图片记录")
                additions += 1
            elif removed:
                additions += 1
            updated["favorite"] = (existing or incoming).get("favorite") is True
            updated["blocked"] = (existing or incoming).get("blocked") is True
            updated["tags"] = _clean_labels(updated.get("tags", []))
            updated["deleted"] = False
            if updated.get("origin") == "online":
                themes: list[str] = []
                for source in (existing or {}, incoming):
                    prior = source.get("themes", [])
                    if not isinstance(prior, list):
                        prior = []
                    for theme in [source.get("theme"), *prior]:
                        if isinstance(theme, str) and theme and theme not in themes:
                            themes.append(theme)
                updated["themes"] = themes
            self.records[record_id] = updated
            if record_id not in self.state["order"]:
                self.state["order"].append(record_id)
            if removed:
                self.state["removed_ids"] = [key for key in self.state.get("removed_ids", []) if key != record_id]
        return additions

    def import_files(
        self, paths: list[Path], album: str = "默认相册", tags: list[str] | None = None,
    ) -> tuple[int, list[str]]:
        count = 0
        errors: list[str] = []
        labels = _clean_labels(tags)
        album = (album or "默认相册").strip()[:100] or "默认相册"
        for index, supplied in enumerate(paths):
            if index >= MAX_FOLDER_FILES:
                errors.append(f"单次最多导入 {MAX_FOLDER_FILES} 张图片，其余图片未处理")
                break
            path = Path(supplied).expanduser().absolute()
            try:
                if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    raise ValueError("不支持此文件类型")
                if _is_link_or_reparse(path):
                    raise ValueError("不导入符号链接或连接点")
                details = path.stat()
                if not stat.S_ISREG(details.st_mode):
                    raise ValueError("请选择普通图片文件")
                if details.st_size > MAX_IMAGE_BYTES:
                    raise ValueError("图片超过 30 MB")
                # Hash and decode exactly these bounded bytes, even if an
                # external program changes the original file during import.
                with path.open("rb") as handle:
                    content = handle.read(MAX_IMAGE_BYTES + 1)
                if len(content) > MAX_IMAGE_BYTES:
                    raise ValueError("图片超过 30 MB")
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(content)) as picture:
                        width, height = picture.size
                        if picture.format not in SUPPORTED_FORMATS:
                            raise ValueError("图片实际格式不受支持")
                        if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
                            raise ValueError(f"图片宽和高至少需要 {MIN_IMAGE_SIDE} 像素")
                        if width * height > MAX_IMAGE_PIXELS:
                            raise ValueError("图片超过 4000 万像素")
                        picture.verify()
                    with Image.open(io.BytesIO(content)) as picture:
                        picture.load()
                record_id = hashlib.sha256(content).hexdigest()
                count += self.add_records([{
                    "id": record_id,
                    "path": str(path),
                    "origin": "local",
                    "title": path.name,
                    "theme": "local",
                    "album": album,
                    "tags": labels,
                    "author": "本地导入",
                    "source_url": "",
                    "license_name": "请自行确认使用权",
                    "license_url": "",
                    "crop_note": "居中裁切为主屏比例（原图不变）",
                    "favorite": False,
                    "blocked": False,
                    "width": width,
                    "height": height,
                }], restore_removed=True)
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
                errors.append(f"{path.name}：{error}")
        if count or paths:
            self.save()
        return count, errors

    def import_folder(
        self, path: Path, album: str = "默认相册", tags: list[str] | None = None,
    ) -> tuple[int, list[str]]:
        """Import just the selected folder's immediate files, never subfolders."""
        folder = Path(path).expanduser().absolute()
        paths: list[Path] = []
        errors: list[str] = []
        try:
            if _is_link_or_reparse(folder):
                raise ValueError("不导入符号链接或连接点目录")
            if not folder.is_dir():
                raise ValueError("请选择图片目录")
            with os.scandir(folder) as entries:
                for entry in entries:
                    candidate = Path(entry.path)
                    if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
                        continue
                    if _is_link_or_reparse(candidate):
                        errors.append(f"{candidate.name}：已跳过符号链接或连接点")
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if len(paths) >= MAX_FOLDER_FILES:
                        errors.append(f"单次最多导入 {MAX_FOLDER_FILES} 张图片，其余图片未处理")
                        break
                    paths.append(candidate)
        except (OSError, ValueError) as error:
            return 0, [f"{folder.name}：{error}"]
        count, file_errors = self.import_files(paths, album=album, tags=tags)
        return count, errors + file_errors

    def eligible(self) -> list[dict[str, Any]]:
        """Apply source, theme, local album/tag, and user-choice filters."""
        mode = self.settings.get("mode", "mixed")
        themes = self.settings.get("themes", [self.settings.get("theme", "landscape")])
        album = self.settings.get("album", "")
        tag = self.settings.get("tag", "")
        favorites = self.settings.get("favorites_only", False)
        result = []
        for record in self.ordered_records():
            local = record.get("origin") == "local"
            if record.get("blocked") or (favorites and not record.get("favorite")):
                continue
            if (mode == "local" and not local) or (mode == "online" and local):
                continue
            if local:
                if album and record.get("album") != album:
                    continue
                if tag and tag not in record.get("tags", []):
                    continue
            else:
                # Selecting a personal album/tag deliberately narrows the
                # collection to local images, even when mode is mixed.
                if album or tag:
                    continue
                available = {record.get("theme"), *record.get("themes", [])}
                if not any(theme in {"", "all"} or theme in available for theme in themes):
                    continue
            result.append(record)
        return result

    def move_records(self, ids: list[str], direction: int,
                     visible_ids: list[str] | None = None) -> int:
        """Move each selected block one place, keeping hidden slots intact."""
        self._normalize_state()
        if not direction:
            return 0
        visible = set(visible_ids) if visible_ids is not None else None
        slots = [index for index, key in enumerate(self.state["order"])
                 if not self.records[key].get("deleted") and (visible is None or key in visible)]
        sequence = [self.state["order"][index] for index in slots]
        original = list(sequence)
        selected = set(ids).intersection(sequence)
        if direction < 0:
            for index in range(1, len(sequence)):
                if sequence[index] in selected and sequence[index - 1] not in selected:
                    sequence[index - 1], sequence[index] = sequence[index], sequence[index - 1]
        else:
            for index in range(len(sequence) - 2, -1, -1):
                if sequence[index] in selected and sequence[index + 1] not in selected:
                    sequence[index], sequence[index + 1] = sequence[index + 1], sequence[index]
        moved = sum(original.index(key) != sequence.index(key) for key in selected)
        if moved:
            for index, key in zip(slots, sequence):
                self.state["order"][index] = key
            self.save()
        return moved

    def remove_records(self, ids: list[str]) -> int:
        """Reversibly remove catalogue entries; never delete any image file."""
        self._normalize_state()
        selected = {key for key in ids if key in self.records and not self.records[key].get("deleted")}
        for key in self.state["order"]:
            if key in selected:
                self.records[key]["deleted"] = True
                if key not in self.state["removed_ids"]:
                    self.state["removed_ids"].append(key)
        if selected:
            self._normalize_state()
            self.save()
        return len(selected)

    def restore_removed(self, ids: list[str]) -> int:
        selected = {key for key in ids if key in self.records and self.records[key].get("deleted")}
        for key in selected:
            self.records[key]["deleted"] = False
        if selected:
            self.state["removed_ids"] = [key for key in self.state.get("removed_ids", []) if key not in selected]
            self.save()
        return len(selected)
