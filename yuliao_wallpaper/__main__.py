from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path


def self_check():
    import tkinter
    import PIL
    from .windows import WindowsAPI
    api = WindowsAPI()
    import pystray
    from . import __version__
    result = {"status": "ok", "version": __version__, "python": sys.version.split()[0],
              "tk": tkinter.TkVersion, "pillow": PIL.__version__, "tray": "available",
              "primary_screen": api.screen_size(), "active_monitors": api.monitor_count(),
              "wallpaper_modified": False}
    print(json.dumps(result, ensure_ascii=True))
    return 0


def smoke_ui(temp_parent=None):
    import tkinter as tk
    from .store import Store
    from .ui import App
    class NoWallpaper:
        def screen_size(self):
            return 1920, 1080
        def capture_original(self):
            raise AssertionError("Smoke check must not change wallpaper")
        apply = capture_original
        restore = capture_original
    with tempfile.TemporaryDirectory(prefix=".smoke-yuliao-", dir=temp_parent) as folder:
        root = tk.Tk()
        app = None
        try:
            app = App(root, Store(Path(folder)), NoWallpaper(), smoke=True)
            root.update_idletasks()
            root.update()
            from .demo import create_demo
            path = Path(folder) / "demo.png"
            create_demo(path)
            count, errors = app.store.import_files([path], "演示相册", ["示例"])
            assert count == 1 and not errors
            app._refresh()
            root.update_idletasks()
            root.update()
            assert app.photo is not None and len(app.tree.get_children()) == 1
            assert not app.rotation.enabled
            assert app.enable_btn.winfo_width() > 120
            width, height = root.winfo_width(), root.winfo_height()
        finally:
            # Close all UI and worker resources even if persistence/assertions
            # fail. An exception must not leave a test window behind.
            if app is not None:
                app._quit()
            else:
                root.destroy()
        print(json.dumps({"status": "ok", "window_constructed": True, "preview_loaded": True,
                          "window_size": [width, height], "wallpaper_modified": False}))
    return 0


def main():
    parser = argparse.ArgumentParser(description="雨聊壁纸：本地优先的 Windows 壁纸工具")
    parser.add_argument("--self-check", action="store_true", help="只读检查依赖和 Windows 接口")
    parser.add_argument("--smoke-ui", action="store_true", help="短暂构建设置窗口与图片预览，不修改桌面")
    parser.add_argument("--data-dir", type=Path, help="指定私人数据目录（通常无需设置）")
    args = parser.parse_args()
    if args.self_check:
        return self_check()
    if args.smoke_ui:
        return smoke_ui(args.data_dir)
    if os.name != "nt":
        raise RuntimeError("本版本仅支持 Windows。")
    import tkinter as tk
    from tkinter import messagebox
    from .store import Store
    from .ui import App
    from .windows import WindowsWallpaper
    # A user-scoped mutex prevents two instances racing on history/backup files.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, "Local\\YuliaoWallpaper-v1")
    if not mutex:
        raise RuntimeError("无法取得应用实例锁，请稍后重试。")
    already_running = ctypes.get_last_error() == 183
    root = tk.Tk()
    if already_running:
        root.withdraw()
        messagebox.showinfo("雨聊壁纸已经运行", "请先在当前版本的托盘菜单中选择「退出」，再打开新版。\n现有壁纸和私人数据保持不变。", parent=root)
        root.destroy()
        kernel.CloseHandle(mutex)
        return 0
    data_root = args.data_dir or Path(os.environ["LOCALAPPDATA"]) / "YuliaoWallpaper"
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        log_dir = data_root / "logs"
        log_dir.mkdir(exist_ok=True)
        handler = RotatingFileHandler(log_dir / "app.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
        logging.basicConfig(level=logging.WARNING, handlers=[handler])
        def callback_error(kind, value, traceback):
            logging.error("UI callback failed", exc_info=(kind, value, traceback))
            messagebox.showerror("操作未完成", str(value), parent=root)
        root.report_callback_exception = callback_error
        app = App(root, Store(data_root), WindowsWallpaper(data_root))
        root.mainloop()
        return 0
    finally:
        kernel.CloseHandle(mutex)


def run():
    """Common source/portable entry; diagnostic failures never open dialogs."""
    try:
        return main()
    except Exception as exc:
        if sys.stderr:
            print(f"Yuliao Wallpaper failed: {exc}", file=sys.stderr)
        if not ("--self-check" in sys.argv or "--smoke-ui" in sys.argv):
            try:
                import tkinter as tk
                from tkinter import messagebox
                fallback = tk.Tk()
                fallback.withdraw()
                messagebox.showerror("雨聊壁纸启动失败", str(exc), parent=fallback)
                fallback.destroy()
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
