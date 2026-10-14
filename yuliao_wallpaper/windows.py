"""Conservative, single-display Windows wallpaper adapter.

No desktop changes happen on import or construction.  The UI must explicitly
capture_original() before the first apply().  Tests inject an API and registry
adapter; they never call the native Windows functions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WallpaperError(RuntimeError):
    """An actionable failure which is safe to display in the Chinese UI."""


class WindowsAPI:
    """Lazy ctypes wrapper; creating an instance only loads system DLLs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WallpaperError("壁纸设置仅支持 Windows。")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SystemParametersInfoW.argtypes = (
            wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT
        )
        self.user32.SystemParametersInfoW.restype = wintypes.BOOL
        self.user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.SendMessageTimeoutW.argtypes = (
            wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        )
        self.user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        self._thread_dpi = getattr(self.user32, "SetThreadDpiAwarenessContext", None)
        if self._thread_dpi is not None:
            self._thread_dpi.argtypes = (ctypes.c_void_p,)
            self._thread_dpi.restype = ctypes.c_void_p

    def get_wallpaper(self) -> str:
        buffer = self.ctypes.create_unicode_buffer(32768)
        if not self.user32.SystemParametersInfoW(0x0073, len(buffer), buffer, 0):
            raise WallpaperError("无法读取当前 Windows 壁纸；未修改桌面。")
        return buffer.value

    def set_wallpaper(self, path: str) -> None:
        pointer = self.ctypes.c_wchar_p(path)
        # Persist first, then notify with a timeout. SPIF_SENDCHANGE can block
        # indefinitely on an unresponsive application's window.
        if not self.user32.SystemParametersInfoW(
            0x0014, 0, self.ctypes.cast(pointer, self.ctypes.c_void_p), 0x01
        ):
            code = self.ctypes.get_last_error()
            raise WallpaperError(f"Windows 未能应用壁纸（错误 {code}）。")
        setting = self.ctypes.create_unicode_buffer("Control Panel\\Desktop")
        result = self.ctypes.c_size_t()
        # A timeout means a window did not acknowledge the notification; it
        # does not mean the successful wallpaper update failed.
        self.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, self.ctypes.addressof(setting),
            0x0002, 500, self.ctypes.byref(result),
        )

    def screen_size(self) -> tuple[int, int]:
        # Read physical pixels without making a process-wide DPI change that
        # could alter Tk's existing interface scaling.
        previous = None
        if self._thread_dpi is not None:
            previous = self._thread_dpi(self.ctypes.c_void_p(-4))
            if not previous:
                previous = self._thread_dpi(self.ctypes.c_void_p(-2))
        try:
            size = (self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1))
        finally:
            if previous:
                self._thread_dpi(previous)
        if min(size) <= 0:
            raise WallpaperError("无法读取主显示器尺寸。")
        return size

    def monitor_count(self) -> int:
        return max(1, self.user32.GetSystemMetrics(80))


class DesktopRegistry:
    """Reads/writes only HKCU\\Control Panel\\Desktop values."""

    KEY = r"Control Panel\Desktop"

    def __init__(self) -> None:
        if os.name != "nt":
            raise WallpaperError("壁纸设置仅支持 Windows。")
        import winreg

        self.winreg = winreg

    def get(self, name: str) -> tuple[Any, int] | None:
        reg = self.winreg
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, self.KEY, 0, reg.KEY_READ) as key:
                return reg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None

    def set(self, name: str, value: Any, kind: int) -> None:
        reg = self.winreg
        with reg.OpenKey(reg.HKEY_CURRENT_USER, self.KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.SetValueEx(key, name, 0, kind, value)

    def delete(self, name: str) -> None:
        reg = self.winreg
        with reg.OpenKey(reg.HKEY_CURRENT_USER, self.KEY, 0, reg.KEY_SET_VALUE) as key:
            try:
                reg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


class WindowsWallpaper:
    """Wallpaper changes with a durable, immutable original snapshot.

    This version intentionally refuses multiple active displays: SPI changes the
    desktop globally and cannot snapshot/restore per-monitor images or slideshows.
    It also does not restore a third-party application's animation/rotation state.

    api: get_wallpaper(), set_wallpaper(str), screen_size(), monitor_count()
    registry: get(name)->(value, registry_type)|None, set(name,value,type), delete(name)
    cache_path is an explicit injection point for the single Windows theme cache;
    no user folders are enumerated. All writes are inside data_root.
    """

    SETTINGS = ("WallpaperStyle", "TileWallpaper")
    CAPTURE_VALUES = SETTINGS + ("Wallpaper", "TranscodedImageCache")
    STYLES = {
        "fill": ("10", "0"), "fit": ("6", "0"),
        "stretch": ("2", "0"), "center": ("0", "0"), "tile": ("0", "1"),
    }
    MAX_IMAGE_BYTES = 256 * 1024 * 1024
    MAX_IMAGE_PIXELS = 100_000_000
    MAX_MANIFEST_BYTES = 128 * 1024

    def __init__(
        self, data_root: Path, *, api: Any = None, registry: Any = None,
        cache_path: Path | None = None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.backup_dir = self._inside(self.data_root / "original_wallpaper")
        self.manifest = self._inside(self.backup_dir / "original.json")
        self._api = api
        self._registry = registry
        self._lock = threading.RLock()
        if cache_path is not None:
            self.cache_path = Path(cache_path)
        elif os.environ.get("APPDATA"):
            self.cache_path = (
                Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Themes"
                / "TranscodedWallpaper"
            )
        else:
            self.cache_path = None

    @property
    def api(self) -> Any:
        if self._api is None:
            self._api = WindowsAPI()
        return self._api

    @property
    def registry(self) -> Any:
        if self._registry is None:
            self._registry = DesktopRegistry()
        return self._registry

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.data_root):
            raise WallpaperError("壁纸数据路径超出了应用数据目录。")
        return resolved

    @staticmethod
    def _encode_registry(value: tuple[Any, int] | None) -> dict[str, Any]:
        if value is None:
            return {"present": False}
        raw, kind = value
        if isinstance(raw, bytes):
            return {"present": True, "type": kind, "binary": base64.b64encode(raw).decode("ascii")}
        return {"present": True, "type": kind, "value": raw}

    def _settings(self, names: tuple[str, ...] = SETTINGS) -> dict[str, Any]:
        return {name: self._encode_registry(self.registry.get(name)) for name in names}

    @staticmethod
    def _cache_matches_current(original: str, settings: dict[str, Any]) -> bool:
        """Reject a stale/unidentified theme cache rather than guess its source.

        Windows' private TranscodedImageCache format is not a supported API.
        Only accept its known 24-byte header + strict UTF-16 source-path layout;
        any future/unknown layout fails closed. This field is read, never changed.
        """
        entry = settings.get("TranscodedImageCache", {})
        if not entry.get("present") or entry.get("type") != 3 or "binary" not in entry:
            return False
        try:
            raw = base64.b64decode(entry["binary"], validate=True)
            if len(raw) < 28 or len(raw) > 65536 or len(raw) % 2:
                return False
            decoded = raw[24:].decode("utf-16-le", errors="strict")
            cached_source, separator, padding = decoded.partition("\0")
            if not separator or not cached_source or padding.strip("\0"):
                return False
            normalize = lambda value: os.path.normcase(os.path.normpath(value))
            return normalize(cached_source) == normalize(original)
        except (ValueError, UnicodeError, TypeError):
            return False

    def _write_settings(self, values: dict[str, Any]) -> None:
        for name in self.SETTINGS:
            entry = values[name]
            if entry["present"]:
                raw = (base64.b64decode(entry["binary"], validate=True)
                       if "binary" in entry else entry["value"])
                self.registry.set(name, raw, entry["type"])
            else:
                self.registry.delete(name)

    @classmethod
    def _image_info(cls, path: Path) -> str:
        try:
            from PIL import Image
            size = path.stat().st_size
            if not path.is_file() or not (0 < size <= cls.MAX_IMAGE_BYTES):
                raise ValueError("文件过大或不是图片文件")
            with Image.open(path) as image:
                if image.width <= 0 or image.height <= 0 or image.width * image.height > cls.MAX_IMAGE_PIXELS:
                    raise ValueError("图片尺寸超出上限")
                image_format = image.format
                image.verify()
            # verify() validates structure; load() also detects truncated pixels.
            with Image.open(path) as image:
                image.load()
            suffix = {"BMP": ".bmp", "JPEG": ".jpg", "PNG": ".png",
                      "GIF": ".gif", "WEBP": ".webp", "TIFF": ".tif"}.get(image_format)
            if suffix is None:
                raise ValueError("不支持的图片格式")
            return suffix
        except Exception as exc:
            raise WallpaperError(f"无法安全读取壁纸图片：{exc}") from exc

    @staticmethod
    def _digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _read_backup(self) -> dict[str, Any]:
        try:
            if self.manifest.stat().st_size > self.MAX_MANIFEST_BYTES:
                raise ValueError("备份记录过大")
            record = json.loads(self.manifest.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("version") != 1 or not isinstance(record.get("original_path"), str):
                raise ValueError("备份记录格式错误")
            values = record["registry"]
            if not isinstance(values, dict):
                raise ValueError("显示设置记录错误")
            for name in self.SETTINGS:
                entry = values[name]
                if not isinstance(entry, dict) or not isinstance(entry.get("present"), bool):
                    raise ValueError("显示设置记录不完整")
                if entry["present"]:
                    if not isinstance(entry["type"], int):
                        raise ValueError("显示设置类型错误")
                    if "binary" in entry:
                        base64.b64decode(entry["binary"], validate=True)
                    elif "value" not in entry:
                        raise ValueError("显示设置值缺失")
            filename = record.get("snapshot_file")
            if record["original_path"]:
                if not isinstance(filename, str) or Path(filename).name != filename:
                    raise ValueError("原壁纸副本路径错误")
                image = self._inside(self.backup_dir / filename)
                if not image.is_file() or image.stat().st_size != record["snapshot_bytes"]:
                    raise ValueError("原壁纸副本缺失或大小不匹配")
                if self._digest(image) != record["snapshot_sha256"]:
                    raise ValueError("原壁纸副本校验失败")
            elif filename is not None:
                raise ValueError("空白桌面的备份记录错误")
            return record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WallpaperError("原壁纸备份缺失或损坏；请保留数据目录，暂不启用换图。") from exc

    def has_backup(self) -> bool:
        with self._lock:
            try:
                self._read_backup()
                return True
            except WallpaperError:
                return False

    def _single_monitor(self) -> None:
        if self.api.monitor_count() != 1:
            raise WallpaperError("首版仅支持单显示器。请先仅启用一个显示器，再试用壁纸切换。")

    @staticmethod
    def _acquire_file_lock(stream: Any) -> None:
        """A kernel-held lock expires when a process exits, including crashes."""
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WallpaperError("另一个进程正在备份原壁纸；请稍后重试。") from exc

    def capture_original(self) -> dict[str, Any]:
        """Commit a validated copy plus settings before any desktop mutation."""
        with self._lock:
            if self.manifest.exists():
                return self._read_backup()
            self._single_monitor()
            self._inside(self.backup_dir).mkdir(parents=True, exist_ok=True)
            lock_path = self._inside(self.backup_dir / ".capture.lock")
            lock_file = lock_path.open("a+b")
            try:
                self._acquire_file_lock(lock_file)
            except Exception:
                lock_file.close()
                raise
            temporary = self._inside(self.backup_dir / "image.pending")
            pending_record = self._inside(self.backup_dir / "original.pending")
            try:
                # A second process may have committed before the lock was taken.
                if self.manifest.exists():
                    return self._read_backup()
                original = self.api.get_wallpaper()
                settings = self._settings(self.CAPTURE_VALUES)
                record: dict[str, Any] = {
                    "version": 1, "captured_at": datetime.now(timezone.utc).isoformat(),
                    "original_path": original, "registry": settings,
                    "snapshot_file": None, "snapshot_source": "blank",
                }
                if original:
                    errors: list[Exception] = []
                    sources = [(Path(original), "original")]
                    if (self.cache_path is not None and Path(original) != self.cache_path
                            and self._cache_matches_current(original, settings)):
                        sources.append((self.cache_path, "windows_theme_cache"))
                    for source, source_kind in sources:
                        try:
                            suffix = self._image_info(source)
                            count = 0
                            with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                                    count += len(chunk)
                                    if count > self.MAX_IMAGE_BYTES:
                                        raise ValueError("原壁纸文件超出备份上限")
                                    outgoing.write(chunk)
                                outgoing.flush()
                                os.fsync(outgoing.fileno())
                            self._image_info(temporary)
                            image = self._inside(self.backup_dir / ("image" + suffix))
                            os.replace(temporary, image)
                            record.update(snapshot_file=image.name, snapshot_source=source_kind,
                                          snapshot_bytes=image.stat().st_size,
                                          snapshot_sha256=self._digest(image))
                            break
                        except (OSError, ValueError, WallpaperError) as exc:
                            errors.append(exc)
                    else:
                        raise WallpaperError("无法备份当前壁纸或 Windows 壁纸缓存；为保证可恢复，未修改桌面。") from errors[-1]
                # If another wallpaper program changed things during copying,
                # retry later rather than committing a mismatched snapshot.
                if self.api.get_wallpaper() != original or self._settings(self.CAPTURE_VALUES) != settings:
                    raise WallpaperError("备份期间桌面壁纸发生变化，请暂停其他壁纸软件后重试。")
                encoded = json.dumps(record, ensure_ascii=False, indent=2)
                if len(encoded.encode("utf-8")) > self.MAX_MANIFEST_BYTES:
                    raise WallpaperError("原壁纸设置记录过大；未修改桌面。")
                with pending_record.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(pending_record, self.manifest)
                return self._read_backup()
            finally:
                lock_file.close()
                # Keep the lock file: deleting it introduces a race where two
                # processes lock distinct file handles for the same filename.
                for path in (temporary, pending_record):
                    path.unlink(missing_ok=True)

    def _archive_backup(self, record: dict[str, Any]) -> Path:
        """Commit a stable history copy before clearing the active session."""
        identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
        archive = self._inside(self.data_root / "original_history" / identifier)
        archive.mkdir(parents=True, exist_ok=False)
        if record["snapshot_file"]:
            source = self._inside(self.backup_dir / record["snapshot_file"])
            target = self._inside(archive / record["snapshot_file"])
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if self._digest(target) != record["snapshot_sha256"]:
                raise WallpaperError("归档的原壁纸副本校验失败；保留当前备份，请重试。")
        archived = dict(record, archived_at=datetime.now(timezone.utc).isoformat())
        pending = self._inside(archive / "original.pending")
        with pending.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(archived, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, self._inside(archive / "original.json"))
        return archive

    def apply(self, image_path: Path, style: str = "fill") -> None:
        """Set only an explicit, prepared image after a verified backup exists."""
        with self._lock:
            self._read_backup()
            self._single_monitor()
            if style not in self.STYLES:
                raise WallpaperError("不支持的壁纸显示方式。")
            path = self._inside(Path(image_path))
            self._image_info(path)
            previous = self._settings()
            try:
                for name, value in zip(self.SETTINGS, self.STYLES[style]):
                    self.registry.set(name, value, 1)  # REG_SZ
                self.api.set_wallpaper(str(path))
            except Exception:
                self._write_settings(previous)
                raise

    def restore(self) -> None:
        """Restore the original pixels and exact style/tile value presence/type."""
        with self._lock:
            record = self._read_backup()
            self._single_monitor()
            archive = self._archive_backup(record)
            target = ""
            if record["original_path"]:
                snapshot = self._inside(self.backup_dir / record["snapshot_file"])
                self._image_info(snapshot)
                # Prefer the original path only if it still has identical bytes.
                # If the source was deleted/edited, restore the private copy.
                source = Path(record["original_path"])
                try:
                    use_original = (source.is_file()
                                    and source.stat().st_size == record["snapshot_bytes"]
                                    and self._digest(source) == record["snapshot_sha256"])
                except OSError:
                    use_original = False
                target = str(source if use_original else self._inside(archive / record["snapshot_file"]))
            previous = self._settings()
            try:
                self._write_settings(record["registry"])
                self.api.set_wallpaper(target)
                # Preserve absent values too, even if Windows recreates them.
                self._write_settings(record["registry"])
            except Exception:
                self._write_settings(previous)
                raise
            # A successful restore ends this session. The archived copy stays
            # available, and next enable captures the then-current wallpaper.
            self.manifest.unlink()

    def screen_size(self) -> tuple[int, int]:
        """Read primary display size; no setting or wallpaper changes."""
        return self.api.screen_size()
