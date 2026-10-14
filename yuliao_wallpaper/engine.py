"""Rotation and image rendering. No network access; OS changes are explicit."""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps


class Rotation:
    def __init__(self, store, wallpaper, clock=time.monotonic, rng=None):
        self.store = store
        self.wallpaper = wallpaper
        self.clock = clock
        self.rng = rng or random.Random()
        self.lock = threading.RLock()
        self.enabled = False
        self.paused = False
        self.due = None
        self.last_error = ""

    def _schedule(self, now=None):
        seconds = float(self.store.settings.get("interval_seconds", 1800))
        if seconds > 0:
            seconds = max(300, seconds)
        self.due = ((self.clock() if now is None else now) + seconds
                    if seconds > 0 and self.enabled and not self.paused else None)

    def reschedule(self):
        with self.lock:
            self._schedule()

    def _eligible(self):
        items = self.store.eligible()
        if not items:
            raise ValueError("当前筛选下没有可用图片。请导入图片或获取精选，并检查相册、标签和收藏筛选。")
        return items

    def enable(self, preferred_id=None):
        with self.lock:
            items = self._eligible()
            item = next((r for r in items if r["id"] == preferred_id), None)
            if preferred_id is not None and item is None:
                raise ValueError("选中的图片不在当前筛选中，请重新选择。")
            item = item or self.rng.choice(items)
            # Render and validate before reading or changing desktop state.
            rendered = self._render(item)
            self.wallpaper.capture_original()
            self.wallpaper.apply(rendered)
            self.enabled = True
            self.paused = False
            self._remember(item["id"])
            self._schedule()
            return item

    def _render(self, item):
        path = Path(item["path"])
        if not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("图片丢失或超出大小限制，当前壁纸保持不变。")
        width, height = self.wallpaper.screen_size()
        if not 100 <= width <= 16000 or not 100 <= height <= 16000 or width * height > 40_000_000:
            raise ValueError("显示器尺寸超出安全范围。")
        with Image.open(path) as original:
            if original.width * original.height > 40_000_000:
                raise ValueError("图片像素过多。")
            original.load()
            oriented = ImageOps.exif_transpose(original)
            rgba = oriented.convert("RGBA")
            backdrop = Image.new("RGBA", rgba.size, (20, 25, 35, 255))
            backdrop.alpha_composite(rgba)
            rendered = ImageOps.fit(backdrop.convert("RGB"), (width, height), Image.Resampling.LANCZOS)
        destination = self.store.data_root / "rendered"
        destination.mkdir(parents=True, exist_ok=True)
        # Alternate names keep the currently referenced BMP intact on failures.
        name = "wallpaper-b.bmp" if self.store.state.get("render_slot") == "a" else "wallpaper-a.bmp"
        output = destination / name
        tmp = output.with_suffix(".tmp")
        rendered.save(tmp, format="BMP")
        tmp.replace(output)
        return output

    def _remember(self, image_id, index=None):
        state = self.store.state
        if index is None:
            history = list(state.get("history", []))[:state.get("history_index", -1) + 1]
            if not history or history[-1] != image_id:
                history.append(image_id)
            history = history[-200:]
            state["history"] = history
            state["history_index"] = len(history) - 1
        else:
            state["history_index"] = index
        state["current_id"] = image_id
        state["render_slot"] = "b" if state.get("render_slot") == "a" else "a"
        seen = list(state.get("seen", []))
        if image_id not in seen:
            seen.append(image_id)
        state["seen"] = seen[-10000:]
        self.store.save()
        self.last_error = ""

    def _apply(self, item, index=None, reset_seen=False):
        rendered = self._render(item)
        self.wallpaper.apply(rendered)
        if reset_seen:
            self.store.state["seen"] = []
        self._remember(item["id"], index)
        return item

    def apply_selected(self, image_id):
        with self.lock:
            if not self.enabled:
                return self.enable(image_id)
            item = next((r for r in self._eligible() if r["id"] == image_id), None)
            if item is None:
                raise ValueError("选中的图片不在当前筛选中。")
            result = self._apply(item)
            self._schedule()
            return result

    def next(self):
        with self.lock:
            if not self.enabled:
                raise ValueError("请先点击「启用 / 应用选中图」。")
            items = self._eligible()
            allowed = {r["id"]: r for r in items}
            state = self.store.state
            history = state.get("history", [])
            for index in range(state.get("history_index", -1) + 1, len(history)):
                if history[index] in allowed:
                    result = self._apply(allowed[history[index]], index)
                    self._schedule()
                    return result
            seen = set(state.get("seen", []))
            pool = [r for r in items if r["id"] not in seen]
            reset_seen = not pool
            if not pool:
                pool = items
            current = state.get("current_id")
            if len(pool) > 1:
                pool = [r for r in pool if r["id"] != current]
            result = self._apply(self.rng.choice(pool), reset_seen=reset_seen)
            self._schedule()
            return result

    def previous(self):
        with self.lock:
            if not self.enabled:
                raise ValueError("请先启用壁纸。")
            allowed = {r["id"]: r for r in self._eligible()}
            history = self.store.state.get("history", [])
            for index in range(self.store.state.get("history_index", -1) - 1, -1, -1):
                if history[index] in allowed:
                    result = self._apply(allowed[history[index]], index)
                    self._schedule()
                    return result
            raise ValueError("已经是可用历史记录的第一张。")

    def pause(self):
        with self.lock:
            self.paused = True
            self.due = None

    def resume(self):
        with self.lock:
            if not self.enabled:
                raise ValueError("请先启用壁纸。")
            self.paused = False
            self._schedule()

    def restore(self):
        with self.lock:
            self.wallpaper.restore()
            self.enabled = False
            self.paused = False
            self.due = None

    def tick(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            if not self.enabled or self.paused or self.due is None or now < self.due:
                return None
            try:
                result = self.next()
            except Exception as exc:
                self.last_error = str(exc)
                # Failed files/network do not cause a retry storm or change current image.
                self.paused = True
                self.due = None
                raise
            self._schedule(now)
            return result
