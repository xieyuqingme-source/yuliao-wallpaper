"""Chinese desktop UI. Tk stays on the main thread; jobs use one serial worker.

Importing this module never creates a window or changes the desktop. Actual
wallpaper changes remain behind an explicit Apply button or an enabled timer.
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageOps, ImageTk

from .commons import CommonsClient
from .engine import Rotation
from .export import export_records


THEMES = {
    "自然风景": "landscape", "城市建筑": "city", "动物": "animals",
    "太空天文": "space", "科学技术": "technology", "海洋海岸": "ocean",
    "花卉植物": "flowers", "山川雪峰": "mountains",
}
MODES = {"混合 · 本地与在线缓存": "mixed", "仅本地图片": "local", "仅在线缓存": "online"}
INTERVALS = {
    "手动": 0, "5 分钟": 300, "15 分钟": 900, "30 分钟": 1800,
    "60 分钟": 3600, "每天 · 每 24 小时": 86400, "自定义": -1,
}
DOWNLOAD_INTERVALS = {"手动下载": 0, "每 6 小时": 6, "每天一次": 24}
DOWNLOAD_COUNTS = {"4 张": 4, "10 张": 10, "20 张": 20, "自定义": -1}
WEBSITES = (
    ("必应每日美图", "https://www.bing.com/", "有下载按钮的图片，可按官方说明下载作壁纸。"),
    ("NASA 地球壁纸", "https://science.nasa.gov/gallery/landsat-virtual-backgrounds-and-wallpapers/",
     "卫星影像壁纸；请查看具体图片署名和使用条款。"),
    ("维基共享精选", "https://commons.wikimedia.org/wiki/Featured_pictures",
     "逐图查看作者、使用许可与署名要求。"),
    ("大都会开放馆藏", "https://www.metmuseum.org/hubs/open-access",
     "选择标有 OA / CC0 的开放图像；并非全站图片都可自由使用。"),
)
WEBSITE_URLS = frozenset(item[1] for item in WEBSITES)
COLORS = {
    "bg": "#090f19", "surface": "#0e1825", "raised": "#162535",
    "accent": "#6bd8cb", "ink": "#dce7ef", "muted": "#9cabbc",
    "line": "#253548", "danger": "#e2aa93",
}
FONT = "Microsoft YaHei UI"


def validate_interval(label: str, minutes: str) -> int:
    """Validate a form without changing saved settings."""
    seconds = INTERVALS[label]
    if seconds == -1:
        value = int(minutes)
        if not 5 <= value <= 10080:
            raise ValueError("自定义间隔应为 5 至 10080 分钟。")
        return value * 60
    return seconds


def validate_download_count(label: str, custom: str) -> int:
    count = DOWNLOAD_COUNTS[label]
    if count == -1:
        count = int(custom)
    if not 1 <= count <= 30:
        raise ValueError("每次下载数量应为 1 至 30 张。")
    return count


def safe_source_url(url: str) -> bool:
    """Metadata links are restricted to the source/known license hosts."""
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https"
                and parsed.hostname in {"commons.wikimedia.org", "creativecommons.org", "www.creativecommons.org"}
                and not parsed.username and not parsed.password and parsed.port in (None, 443))
    except (ValueError, TypeError):
        return False


def selection_focus(selected: tuple[str, ...], focus: str, fallback: str | None = None) -> str | None:
    """A multi-selection previews/applies its focused item, never an arbitrary one."""
    if focus in selected:
        return focus
    return selected[0] if selected else fallback


def toolbar_positions(sizes, width, gap=6, row_gap=4):
    """Wrap measured buttons with independent row coordinates (no shared columns)."""
    positions = []
    x = y = row_height = 0
    for requested_width, requested_height in sizes:
        if x and x + requested_width > width:
            x, y, row_height = 0, y + row_height + row_gap, 0
        positions.append((x, y, requested_width, requested_height))
        x += requested_width + gap
        row_height = max(row_height, requested_height)
    return positions, y + row_height + (row_gap if positions else 0)


class App:
    def __init__(self, root, store, wallpaper, smoke=False):
        self.root, self.store = root, store
        self.rotation = Rotation(store, wallpaper)
        self.client = CommonsClient(store.data_root / "cache")
        self.smoke = smoke
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wallpaper")
        self.events = queue.Queue()
        self.busy = False
        self.closing = False
        self.job_kind = ""
        self.job_background = False
        self.cancel_event = None
        self.tray = None
        self.selected_id = None
        self.last_download = time.monotonic()
        self.photo = None
        self._preview_after = None
        self._preview_key = None
        self._job_buttons = []
        self._toolbars = []
        self._control_rows = []
        self.root.title("雨聊壁纸")
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(1100, max(720, screen_width - 100)), min(760, max(540, screen_height - 110))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(860, width), min(600, height))
        self.root.configure(bg=COLORS["bg"])
        self._style()
        self.status = tk.StringVar(value="尚未启用 · 导入图片或获取精选，再点击“启用 / 应用”。")
        self.count = tk.StringVar()
        self.selection_count = tk.StringVar()
        self.activity = tk.StringVar(value="待机")
        self.mode = tk.StringVar(value=self._label(MODES, store.settings.get("mode", "mixed")))
        selected_themes = store.settings.get("themes") or [store.settings.get("theme", "landscape")]
        self.theme_vars = {key: tk.BooleanVar(value=key in selected_themes) for key in THEMES.values()}
        sec = store.settings.get("interval_seconds", 1800)
        self.interval = tk.StringVar(value=self._label(INTERVALS, sec, "自定义"))
        self.minutes = tk.StringVar(value=str(max(5, int(sec / 60))) if sec else "60")
        self.download = tk.StringVar(value=self._label(DOWNLOAD_INTERVALS, store.settings.get("download_hours", 6)))
        download_count = store.settings.get("download_count", 10)
        self.download_count = tk.StringVar(value=self._label(DOWNLOAD_COUNTS, download_count, "自定义"))
        self.custom_count = tk.StringVar(value=str(download_count))
        self.album_filter = tk.StringVar(value=store.settings.get("album", ""))
        self.tag_filter = tk.StringVar(value=store.settings.get("tag", ""))
        self.import_album = tk.StringVar(value="默认相册")
        self.import_tags = tk.StringVar()
        self.favorites = tk.BooleanVar(value=store.settings.get("favorites_only", False))
        self.show_blocked = tk.BooleanVar(value=False)
        self.show_removed = tk.BooleanVar(value=False)
        self._build()
        self._refresh()
        if store.status:
            self.status.set(store.status)
        self.root.protocol("WM_DELETE_WINDOW", self._close_window)
        self.root.after(150, self._poll)
        if not smoke:
            self.root.after(300, self._start_tray)

    @staticmethod
    def _label(mapping, value, fallback=None):
        return next((k for k, v in mapping.items() if v == value), fallback or next(iter(mapping)))

    def _style(self):
        c = COLORS
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=c["bg"], foreground=c["ink"], font=(FONT, 10))
        style.configure("TFrame", background=c["bg"])
        style.configure("Card.TFrame", background=c["surface"])
        style.configure("TLabel", background=c["bg"], foreground=c["ink"])
        style.configure("Muted.TLabel", foreground=c["muted"])
        style.configure("Card.TLabel", background=c["surface"])
        style.configure("CardMuted.TLabel", background=c["surface"], foreground=c["muted"])
        style.configure("Section.TLabel", background=c["surface"], foreground=c["accent"], font=(FONT, 11, "bold"))
        style.configure("TButton", background=c["surface"], foreground=c["ink"], bordercolor=c["line"],
                        lightcolor=c["line"], darkcolor=c["line"], padding=(11, 7), relief="flat", font=(FONT, 9))
        style.map("TButton", background=[("active", c["raised"]), ("disabled", c["surface"])],
                  foreground=[("disabled", "#596c80")], bordercolor=[("focus", c["accent"])])
        style.configure("Accent.TButton", background=c["accent"], foreground=c["bg"], bordercolor=c["accent"])
        style.map("Accent.TButton", background=[("active", "#93e6dc"), ("disabled", "#315554")],
                  foreground=[("disabled", "#a1b3b9")])
        style.configure("Danger.TButton", foreground=c["danger"])
        style.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, 0, 0, 8))
        style.configure("TNotebook.Tab", background=c["surface"], foreground=c["muted"], padding=(18, 9))
        style.map("TNotebook.Tab", background=[("selected", c["raised"])], foreground=[("selected", c["accent"])])
        style.configure("Treeview", background=c["surface"], fieldbackground=c["surface"], foreground=c["ink"],
                        bordercolor=c["line"], rowheight=33, font=(FONT, 9))
        style.map("Treeview", background=[("selected", "#23434a")], foreground=[("selected", "#ecffff")])
        style.configure("Treeview.Heading", background=c["raised"], foreground=c["muted"],
                        padding=(7, 8), bordercolor=c["line"], font=(FONT, 9))
        style.map("Treeview.Heading", background=[("active", c["raised"])])
        style.configure("TEntry", fieldbackground=c["bg"], foreground=c["ink"], bordercolor=c["line"], padding=6,
                        insertcolor=c["ink"])
        style.configure("TCombobox", fieldbackground=c["bg"], background=c["raised"], foreground=c["ink"],
                        bordercolor=c["line"], arrowcolor=c["muted"], padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", c["bg"])], foreground=[("readonly", c["ink"])])
        style.configure("TSpinbox", fieldbackground=c["bg"], background=c["raised"], foreground=c["ink"],
                        bordercolor=c["line"], arrowcolor=c["muted"], padding=5)
        style.configure("TCheckbutton", background=c["bg"], foreground=c["muted"], padding=(0, 4))
        style.configure("Card.TCheckbutton", background=c["surface"])
        style.map("TCheckbutton", background=[("active", c["bg"])], foreground=[("selected", c["ink"])])
        style.map("Card.TCheckbutton", background=[("active", c["surface"])])
        style.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["surface"],
                        bordercolor=c["line"], lightcolor=c["accent"], darkcolor=c["accent"])
        style.configure("TScrollbar", background=c["line"], troughcolor=c["surface"],
                        bordercolor=c["surface"], arrowcolor=c["muted"])
        # Limit dropdown colors to this root. No operating-system theme changes.
        self.root.option_add("*TCombobox*Listbox.background", c["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["ink"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#23434a")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

    def _build(self):
        outer = ttk.Frame(self.root, padding=(20, 16, 20, 12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="雨聊壁纸", foreground=COLORS["accent"], font=(FONT, 23, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="YULIAO / 0.2", style="Muted.TLabel", font=("Consolas", 9)).grid(row=0, column=1, sticky="e")
        ttk.Label(head, text="为桌面留一幅喜欢的风景", style="Muted.TLabel", font=(FONT, 9)).grid(row=1, column=0, sticky="w", pady=(3, 0))
        note = ttk.Label(outer, text="试用前请自行暂停其他壁纸软件，避免同时换图。本版支持一个活动显示器。", style="Muted.TLabel", font=(FONT, 9))
        note.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._wrap_label(note, outer, subtract=40)
        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        library = ttk.Frame(self.notebook, padding=(0, 7, 0, 0))
        settings = ttk.Frame(self.notebook)
        websites = ttk.Frame(self.notebook)
        self.notebook.add(library, text="我的图库")
        self.notebook.add(settings, text="来源与设置")
        self.notebook.add(websites, text="推荐网站")
        self._build_library(library)
        self._build_settings(settings)
        self._build_websites(websites)
        foot = ttk.Frame(outer)
        foot.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        foot.columnconfigure(0, weight=1)
        status_label = ttk.Label(foot, textvariable=self.status, foreground=COLORS["accent"], font=(FONT, 9))
        status_label.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self._wrap_label(status_label, foot, subtract=100)
        ttk.Button(foot, text="退出", command=self._quit).grid(row=0, column=1, sticky="e")
        jobbar = ttk.Frame(outer)
        jobbar.grid(row=4, column=0, sticky="ew", pady=(5, 0))
        jobbar.columnconfigure(0, weight=1)
        ttk.Label(jobbar, textvariable=self.activity, style="Muted.TLabel", font=(FONT, 8)).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(jobbar, mode="indeterminate", length=110)
        self.progress_bar.grid(row=0, column=1, padx=(0, 10), sticky="e")
        self.cancel_btn = ttk.Button(jobbar, text="取消任务", command=self._cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=2, sticky="e")

    def _build_library(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        panes = tk.PanedWindow(parent, orient="horizontal", bg=COLORS["bg"], borderwidth=0,
                              sashwidth=9, sashpad=3, sashrelief="flat", showhandle=False)
        panes.grid(row=0, column=0, sticky="nsew")
        preview_host = ttk.Frame(panes)
        preview_host.columnconfigure(0, weight=1)
        preview_host.rowconfigure(0, weight=1)
        panes.add(preview_host, minsize=260, stretch="always")
        self.preview = tk.Canvas(preview_host, width=470, height=290, bg=COLORS["surface"],
                                 highlightbackground=COLORS["line"], highlightthickness=1)
        self.preview.grid(row=0, column=0, sticky="nsew")
        self.preview.bind("<Configure>", lambda event: self._queue_preview())
        self.caption = ttk.Label(preview_host, text="选择图片预览", style="Muted.TLabel", font=(FONT, 9))
        self.caption.grid(row=1, column=0, sticky="ew", pady=(10, 10))
        self._wrap_label(self.caption, preview_host)
        self.enable_btn = self._button(preview_host, "启用 / 应用", self._enable, style="Accent.TButton")
        self.enable_btn.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        controls = self._toolbar(preview_host, [
            ("上一张", lambda: self._job(self.rotation.previous, "已切换上一张", kind="rotation")),
            ("下一张", lambda: self._job(self.rotation.next, "已切换下一张", kind="rotation")),
            ("暂停换图", self._pause),
            ("恢复原壁纸", lambda: self._job(self.rotation.restore, "已恢复启用前原壁纸", kind="rotation")),
        ], row=3)
        self.pause_btn = controls[2]
        local_note = ttk.Label(preview_host, text="预览不改桌面 · 应用时居中裁切，原图不变", style="Muted.TLabel", font=(FONT, 8))
        local_note.grid(row=4, column=0, sticky="ew", pady=(8, 2))
        self._wrap_label(local_note, preview_host)
        gallery = ttk.Frame(panes)
        gallery.columnconfigure(0, weight=1)
        gallery.rowconfigure(2, weight=1)
        panes.add(gallery, minsize=350, stretch="always")
        self._toolbar(gallery, [("导入图片", self._files), ("导入文件夹", self._folder), ("载入内置示例", self._demo)], row=0)
        top = ttk.Frame(gallery)
        top.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="图片库", font=(FONT, 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(top, textvariable=self.selection_count, style="Muted.TLabel", font=(FONT, 8)).grid(row=0, column=1, sticky="e")
        tree_frame = ttk.Frame(gallery)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_frame, columns=("origin", "mark"), show="tree headings", height=6, selectmode="extended")
        self.tree.heading("#0", text="图片 / 相册")
        self.tree.column("#0", width=245, minwidth=130, stretch=True)
        self.tree.heading("origin", text="来源")
        self.tree.column("origin", width=55, minwidth=50, stretch=False)
        self.tree.heading("mark", text="标记")
        self.tree.column("mark", width=88, minwidth=68, stretch=False)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._select)
        # Double-click only previews; Apply is always a distinct action.
        self.tree.bind("<Double-1>", self._select)
        self._toolbar(gallery, [
            ("上移", lambda: self._move("up")), ("下移", lambda: self._move("down")),
            ("收藏 / 取消", lambda: self._mark("favorite")),
            ("不再显示 / 解除", lambda: self._mark("blocked")),
        ], row=3, pady=(8, 0))
        self._toolbar(gallery, [
            ("保存选中图片", self._export), ("从图库移除", self._remove, "Danger.TButton"),
            ("恢复选中", self._restore_removed), ("作者与许可", self._source),
        ], row=4, pady=(6, 0))
        filters = ttk.Frame(gallery)
        filters.grid(row=5, column=0, sticky="ew", pady=(7, 0))
        ttk.Checkbutton(filters, text="显示已屏蔽", variable=self.show_blocked, command=self._refresh).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(filters, text="显示已移除", variable=self.show_removed, command=self._refresh).pack(side="left")
        count_label = ttk.Label(gallery, textvariable=self.count, style="Muted.TLabel", font=(FONT, 8))
        count_label.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self._wrap_label(count_label, gallery)

    def _scroll_page(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        canvas = tk.Canvas(parent, bg=COLORS["bg"], highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        body = ttk.Frame(canvas, padding=(0, 5, 8, 5))
        window = canvas.create_window(0, 0, window=body, anchor="nw")
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        def wheel(event):
            # Bind to this root, then restrict to descendants of this page.
            current = event.widget
            while current is not None:
                if current == parent:
                    if event.delta:
                        canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                    return
                current = getattr(current, "master", None)
        self.root.bind("<MouseWheel>", wheel, add="+")
        return body

    def _build_settings(self, parent):
        body = self._scroll_page(parent)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        left, right = ttk.Frame(body), ttk.Frame(body)
        left.grid(row=0, column=0, sticky="new", padx=(0, 7))
        right.grid(row=0, column=1, sticky="new", padx=(7, 0))
        def columns(event):
            # Requested widths use the current font/DPI, so high scaling also
            # stacks the two settings columns before controls become cramped.
            requested = max((row.winfo_reqwidth() for row in self._control_rows), default=340)
            compact = event.width < max(760, 2 * (requested + 32) + 28)
            right.grid_configure(row=1 if compact else 0, column=0 if compact else 1,
                                 padx=0 if compact else (7, 0))
            left.grid_configure(padx=0 if compact else (0, 7))
            body.columnconfigure(1, weight=0 if compact else 1)
        body.bind("<Configure>", columns, add="+")
        source = self._section(left, "01 / 精选来源")
        self._combo(source, "使用来源", self.mode, MODES)
        self._note(source, "主题可多选，在线图片取各主题并集；本地照片按相册与标签筛选。")
        themes = ttk.Frame(source, style="Card.TFrame")
        themes.pack(fill="x", pady=(5, 8))
        for index, (label, key) in enumerate(THEMES.items()):
            ttk.Checkbutton(themes, text=label, variable=self.theme_vars[key], style="Card.TCheckbutton",
                            command=self._settings).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 18))
        themes.columnconfigure(0, weight=1)
        themes.columnconfigure(1, weight=1)
        self._control_rows.append(themes)
        self._combo(source, "每次下载", self.download_count, DOWNLOAD_COUNTS)
        self._spin(source, "自定义张数", self.custom_count, 1, 30, 1)
        self.download_btn = self._button(source, "获取 Commons 精选", self._download, style="Accent.TButton")
        self.download_btn.pack(fill="x", pady=(10, 6))
        self._note(source, "随机挑选开放许可横图，数量是上限。串行低频下载；取消保留已完成图片，断网可用缓存。科学技术主要含真实天文、仪器与技术图像。")
        local = self._section(left, "02 / 本地导入")
        self._entry(local, "导入至相册", self.import_album)
        self._entry(local, "导入标签", self.import_tags)
        self._note(local, "标签用逗号分隔。导入文件夹只读取当前层，不扫描子目录、不上传、不修改原图。图片与文件夹入口在“我的图库”。")
        schedule = self._section(right, "03 / 换图与下载")
        self._combo(schedule, "换图间隔", self.interval, INTERVALS)
        self._spin(schedule, "自定义分钟", self.minutes, 5, 10080, 5)
        self._combo(schedule, "下载间隔", self.download, DOWNLOAD_INTERVALS)
        self._note(schedule, "换图与下载分别计时，最短换图间隔 5 分钟。自动下载在启用后运行；暂停只暂停换图。睡眠唤醒只补一次，不密集追赶。")
        filtering = self._section(right, "04 / 图库筛选")
        self._entry(filtering, "相册筛选", self.album_filter)
        self._entry(filtering, "标签筛选", self.tag_filter)
        ttk.Checkbutton(filtering, text="只显示收藏", variable=self.favorites, style="Card.TCheckbutton", command=self._settings).pack(anchor="w", pady=(4, 3))
        self._note(filtering, "相册和标签留空显示全部本地图片；填写任一项时，只查看匹配的本地图片。图库上移 / 下移只调整列表顺序，自动轮播保持随机。")
        self._button(filtering, "保存设置 / 筛选", self._settings, style="Accent.TButton").pack(fill="x", pady=(10, 0))
        safety = self._section(right, "05 / 本地与恢复")
        self._note(safety, "首次应用前保存原壁纸与显示设定。退出保留当前壁纸；如需恢复，请在图库点击“恢复原壁纸”。开机启动未启用。")

    def _build_websites(self, parent):
        body = self._scroll_page(parent)
        header = ttk.Label(body, text="去官网挑选，再带回你的图库", font=(FONT, 16, "bold"), foreground=COLORS["accent"])
        header.pack(anchor="w", pady=(12, 9))
        intro = ttk.Label(body, text="点击后会在默认浏览器打开官网。自行下载喜欢的图片，再通过“导入图片”加入；请遵守每张图片的使用许可。", style="Muted.TLabel")
        intro.pack(fill="x", pady=(0, 12))
        self._wrap_label(intro, body)
        for name, url, note in WEBSITES:
            section = self._section(body, name)
            self._note(section, note)
            ttk.Button(section, text="访问官网 ↗", command=lambda target=url: self._open_website(target)).pack(anchor="w", pady=(9, 0))
        footer = ttk.Label(body, text="网站仅作推荐，应用不代抓图片，也不表示与这些网站存在合作关系。", style="Muted.TLabel", font=(FONT, 9))
        footer.pack(fill="x", pady=(6, 12))
        self._wrap_label(footer, body)

    def _section(self, parent, title):
        frame = ttk.Frame(parent, padding=16, style="Card.TFrame")
        frame.pack(fill="x", pady=(0, 12))
        ttk.Label(frame, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        return frame

    def _note(self, frame, text):
        label = ttk.Label(frame, text=text, style="CardMuted.TLabel", font=(FONT, 9), justify="left")
        label.pack(fill="x", pady=(5, 0))
        self._wrap_label(label, frame, subtract=40)

    def _wrap_label(self, label, host, subtract=8):
        # Actual allocated width accounts for window resizing and Tk/DPI scaling.
        host.bind("<Configure>", lambda event: label.configure(wraplength=max(150, event.width - subtract)), add="+")
        label.configure(wraplength=350)

    def _button(self, frame, text, command, style="TButton"):
        button = ttk.Button(frame, text=text, command=command, style=style)
        self._job_buttons.append(button)
        return button

    def _toolbar(self, parent, items, row, pady=(0, 0)):
        toolbar = ttk.Frame(parent)
        toolbar.grid(row=row, column=0, sticky="ew", pady=pady)
        buttons = []
        for entry in items:
            if isinstance(entry, tuple):
                text, command, *styles = entry
                button = self._button(toolbar, text, command, styles[0] if styles else "TButton")
            else:
                button = entry
            buttons.append(button)
        def reflow(event=None):
            width = max(220, toolbar.winfo_width())
            sizes = [(max(65, button.winfo_reqwidth()), button.winfo_reqheight()) for button in buttons]
            positions, height = toolbar_positions(sizes, width)
            for button, (x, y, button_width, button_height) in zip(buttons, positions):
                button.place(x=x, y=y, width=button_width, height=button_height)
            toolbar.configure(height=height)
        toolbar.bind("<Configure>", reflow)
        self.root.after_idle(reflow)
        self._toolbars.append((toolbar, reflow))
        return buttons

    def _combo(self, frame, label, variable, values):
        row = ttk.Frame(frame, style="Card.TFrame")
        self._control_rows.append(row)
        row.pack(fill="x", pady=4)
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text=label, style="Card.TLabel", font=(FONT, 9)).grid(row=0, column=0, sticky="w", padx=(0, 12))
        combo = ttk.Combobox(row, textvariable=variable, values=list(values), state="readonly", width=16)
        combo.grid(row=0, column=1, sticky="ew")
        combo.bind("<<ComboboxSelected>>", lambda event: self._settings())

    def _entry(self, frame, label, variable):
        row = ttk.Frame(frame, style="Card.TFrame")
        self._control_rows.append(row)
        row.pack(fill="x", pady=4)
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text=label, style="Card.TLabel", font=(FONT, 9)).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(row, textvariable=variable, width=15).grid(row=0, column=1, sticky="ew")

    def _spin(self, frame, label, variable, low, high, increment):
        row = ttk.Frame(frame, style="Card.TFrame")
        self._control_rows.append(row)
        row.pack(fill="x", pady=4)
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text=label, style="Card.TLabel", font=(FONT, 9)).grid(row=0, column=0, sticky="w", padx=(0, 12))
        spin = ttk.Spinbox(row, from_=low, to=high, increment=increment, textvariable=variable, width=10)
        spin.grid(row=0, column=1, sticky="ew")
        spin.bind("<FocusOut>", lambda event: self._settings())
        spin.bind("<Return>", lambda event: self._settings())

    def _chosen_themes(self):
        return [key for key, variable in self.theme_vars.items() if variable.get()]

    def _settings(self, announce=True):
        if self.busy or self.closing:
            if announce:
                self.status.set("任务进行中；完成后可保存新的设置。")
            return False
        try:
            themes = self._chosen_themes()
            if not themes:
                raise ValueError("请至少选择一个在线主题。")
            values = {
                "mode": MODES[self.mode.get()], "theme": themes[0], "themes": themes,
                "interval_seconds": validate_interval(self.interval.get(), self.minutes.get()),
                "download_hours": DOWNLOAD_INTERVALS[self.download.get()],
                "download_count": validate_download_count(self.download_count.get(), self.custom_count.get()),
                "album": self.album_filter.get().strip()[:120], "tag": self.tag_filter.get().strip()[:80],
                "favorites_only": self.favorites.get(),
            }
            with self.rotation.lock:
                previous = self.store.settings.copy()
                self.store.settings.update(values)
                try:
                    self.store.save()
                except Exception:
                    self.store.settings.clear()
                    self.store.settings.update(previous)
                    raise
            if previous.get("interval_seconds") != values["interval_seconds"]:
                self.rotation.reschedule()
            if announce:
                self.status.set("设置已保存 · 筛选同步到图库；尚未启用时，桌面保持不变。")
            self._refresh()
        except (ValueError, KeyError, OSError) as exc:
            self.status.set("设置未保存：" + str(exc))
            return False
        return True

    def _job(self, fn, success, *, kind="operation", cancellable=False, background=False):
        if self.busy or self.closing:
            return False
        self.busy, self.job_kind, self.job_background = True, kind, background
        self.cancel_event = threading.Event() if cancellable else None
        self.status.set("正在处理，请稍候…")
        self.activity.set({"download": "正在串行获取精选图片", "export": "正在保存选中图片", "rotation": "正在应用壁纸"}.get(kind, "正在处理本地图片"))
        self._buttons()
        self.progress_bar.start(80)
        def work():
            try:
                self.events.put(("done", success, fn()))
            except Exception as exc:
                self.events.put(("error", str(exc), None))
        try:
            self.executor.submit(work)
        except RuntimeError as exc:
            self.events.put(("error", str(exc), None))
        return True

    def _cancel(self):
        if self.busy and self.cancel_event is not None:
            self.cancel_event.set()
            self.status.set("已请求取消；正在结束当前读取，已完成的图片会保留。")
            self.activity.set("取消中 · 单个网络请求最多等待 15 秒")
            self.cancel_btn.configure(state="disabled")

    def _enable(self):
        if self.busy or not self._settings(announce=False):
            return
        image_id = self.selected_id
        item = self.store.records.get(image_id)
        if item and (item.get("deleted") or item.get("blocked")):
            self.status.set("请先恢复已移除图片或解除屏蔽，再应用。")
            return
        if not self.rotation.enabled:
            text = "将先保存原壁纸与显示设定，再应用当前选中图片。\n请自行暂停其他壁纸软件，避免同时换图。\n之后可点击“恢复原壁纸”。"
            if not messagebox.askokcancel("启用并应用壁纸", text, parent=self.root):
                return
        self._job(lambda: self.rotation.apply_selected(image_id) if image_id else self.rotation.enable(), "已应用壁纸 · 原壁纸可恢复", kind="rotation")

    def _pause(self):
        # Network/export work does not own the desktop. Pause remains immediate
        # while these jobs run; an in-flight wallpaper apply finishes first.
        if self.busy and self.job_kind not in {"download", "export"}:
            return
        try:
            if self.rotation.paused and self.rotation.enabled:
                self.rotation.resume()
                self.status.set("自动换图已恢复，重新计时。")
            elif self.rotation.enabled:
                self.rotation.pause()
                self.status.set("已暂停换图，当前壁纸保留；下载按独立设置运行。")
            else:
                self.status.set("尚未启用，请先选择图片并应用。")
            self._buttons()
        except ValueError as exc:
            self.status.set(str(exc))

    def _download(self, auto=False):
        if self.busy or self.closing:
            return
        if not auto and not self._settings(announce=False):
            return
        themes = tuple(self.store.settings.get("themes") or [self.store.settings.get("theme", "landscape")])
        quantity = self.store.settings.get("download_count", 10)
        cancel = threading.Event()
        def task():
            records = self.client.fetch_many(themes, limit=quantity, cancel_event=cancel,
                                             progress=lambda text: self.events.put(("progress", str(text), None)))
            with self.rotation.lock:
                count = self.store.add_records(records)
                self.store.save()
            stats = getattr(self.client, "last_stats", {})
            return {"download": count, "message": getattr(self.client, "last_status", "") or getattr(self.client, "last_error", ""),
                    "cancelled": stats.get("cancelled", cancel.is_set())}
        # Timestamp the attempt, including errors, to prevent automatic retry storms.
        self.last_download = time.monotonic()
        if self._job(task, "精选图库已更新", kind="download", cancellable=True, background=auto):
            self.cancel_event = cancel

    def _import(self, paths=None, folder=None):
        album = self.import_album.get().strip() or "默认相册"
        tags = [value.strip() for value in self.import_tags.get().replace("，", ",").split(",") if value.strip()][:20]
        def task():
            with self.rotation.lock:
                count, errors = (self.store.import_folder(folder, album, tags) if folder else self.store.import_files(paths, album, tags))
                return {"import": count, "errors": errors}
        self._job(task, "本地导入完成")

    def _files(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="选择本地图片（可多选，不上传）",
                                          filetypes=[("支持的图片", "*.jpg *.jpeg *.png *.webp *.bmp"), ("所有文件", "*.*")])
        if paths:
            self._import(paths=[Path(path) for path in paths])

    def _folder(self):
        if self.busy:
            return
        path = filedialog.askdirectory(parent=self.root, title="只导入选定文件夹当前层，不扫描子目录")
        if path:
            self._import(folder=Path(path))

    def _demo(self):
        from .demo import create_demo
        path = self.store.data_root / "demo" / "first-light.png"
        def task():
            create_demo(path)
            with self.rotation.lock:
                result = self.store.import_files([path], "演示相册", ["风景", "程序示例"])
                for record in self.store.records.values():
                    if Path(record["path"]) == path:
                        record.update(title="初光 · 内置演示", author="雨聊壁纸程序绘制示例", license_name="程序演示图片")
                self.store.save()
            return {"import": result[0], "errors": result[1]}
        self._job(task, "示例已载入 · 选择图片预览，再点击启用")

    def _selected_ids(self):
        selected = set(self.tree.selection())
        return tuple(row for row in self.tree.get_children() if row in selected and row in self.store.records)

    def _refresh(self, focus_id=None):
        old_selected = self._selected_ids()
        old_focus = focus_id or self.selected_id
        with self.rotation.lock:
            eligible = {record["id"] for record in self.store.eligible()}
            rows = [record for record in self.store.ordered_records(include_removed=True)
                    if ((not record.get("deleted") and (record["id"] in eligible or (self.show_blocked.get() and record.get("blocked"))))
                        or (self.show_removed.get() and record.get("deleted")))]
            active_total = sum(not record.get("deleted") for record in self.store.records.values())
        for row in self.tree.get_children():
            self.tree.delete(row)
        for record in rows:
            marks = []
            if record.get("favorite"):
                marks.append("★")
            if record.get("deleted"):
                marks.append("已移除")
            elif record.get("blocked"):
                marks.append("已屏蔽")
            album = record.get("album") or self._label(THEMES, record.get("theme"), "精选")
            title = f"{record['title']}  ·  {album}"
            self.tree.insert("", "end", iid=record["id"], text=title,
                             values=("本地" if record["origin"] == "local" else "在线", " ".join(marks)))
        available = {record["id"] for record in rows}
        current = self.store.state.get("current_id")
        candidate = old_focus if old_focus in available else current if current in available else rows[0]["id"] if rows else None
        selected = (candidate,) if focus_id and candidate else tuple(value for value in old_selected if value in available)
        if not selected and candidate:
            selected = (candidate,)
        self.selected_id = selection_focus(selected, candidate)
        if selected:
            self.tree.selection_set(selected)
            self.tree.focus(self.selected_id)
            self.tree.see(self.selected_id)
        self.count.set(f"当前列表 {len(rows)} 张 · 图库 {active_total} 张 · Ctrl / Shift 可多选")
        self.selection_count.set(f"已选 {len(selected)} 张")
        self._queue_preview()
        self._buttons()

    def _select(self, event=None):
        selected = self._selected_ids()
        self.selected_id = selection_focus(selected, self.tree.focus())
        self.selection_count.set(f"已选 {len(selected)} 张")
        self._queue_preview()

    def _queue_preview(self):
        if self._preview_after is not None:
            self.root.after_cancel(self._preview_after)
        self._preview_after = self.root.after(60, self._preview)

    def _preview(self):
        self._preview_after = None
        width = max(120, self.preview.winfo_width())
        height = max(120, self.preview.winfo_height())
        item = self.store.records.get(self.selected_id)
        key = (self.selected_id, width, height)
        if key == self._preview_key:
            return
        self._preview_key = key
        self.preview.delete("all")
        if not item:
            self.photo = None
            self.preview.create_text(width / 2, height / 2, text="为桌面挑一幅风景\n\n导入图片，或在“来源与设置”获取精选", width=max(120, width - 40),
                                     justify="center", font=(FONT, 12), fill=COLORS["muted"])
            self.caption.configure(text="预览保持原图比例，不修改桌面。")
            return
        try:
            path = Path(item["path"])
            if not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
                raise ValueError("图片不可用")
            with Image.open(path) as original:
                if original.width * original.height > 40_000_000:
                    raise ValueError("图片过大")
                original.draft("RGB", (width, height))
                image = ImageOps.exif_transpose(original)
                image.thumbnail((max(1, width - 24), max(1, height - 24)), Image.Resampling.LANCZOS)
                self.photo = ImageTk.PhotoImage(image)
            self.preview.create_image(width / 2, height / 2, image=self.photo)
            note = "已移除，可恢复" if item.get("deleted") else "已屏蔽" if item.get("blocked") else "仅预览"
            self.caption.configure(text=f"{item['title'][:120]}\n{note} · {'本地图片' if item['origin'] == 'local' else 'Commons 精选'}")
        except (OSError, ValueError, Image.DecompressionBombError):
            self.photo = None
            self.preview.create_text(width / 2, height / 2, text="图片预览不可用\n文件可能已移动或无法读取", width=max(120, width - 40),
                                     justify="center", fill=COLORS["danger"], font=(FONT, 11))
            self.caption.configure(text=f"{item['title'][:120]} · 预览不可用")

    def _mark(self, key):
        if self.busy:
            return
        ids = self._selected_ids()
        if not ids:
            self.status.set("请先在图库中选择图片。")
            return
        def task():
            with self.rotation.lock:
                value = not all(self.store.records[image_id].get(key) for image_id in ids)
                for image_id in ids:
                    self.store.records[image_id][key] = value
                self.store.save()
            return {"changed": len(ids)}
        self._job(task, "标记已保存 · 当前桌面保持到下一次换图")

    def _move(self, direction):
        if self.busy:
            return
        ids, visible = self._selected_ids(), tuple(self.tree.get_children())
        if not ids:
            self.status.set("请先选择需要移动的图片。")
            return
        def task():
            with self.rotation.lock:
                self.store.move_records(ids, -1 if direction == "up" else 1, visible_ids=visible)
        self._job(task, "图库顺序已调整 · 自动轮播保持随机")

    def _remove(self):
        if self.busy:
            return
        ids = self._selected_ids()
        if not ids:
            self.status.set("请先选择要移除的图片。")
            return
        def task():
            with self.rotation.lock:
                count = self.store.remove_records(ids)
            return {"removed": count}
        self._job(task, "已从图库移除 · 原文件与收藏保留，可显示已移除后恢复")

    def _restore_removed(self):
        if self.busy:
            return
        ids = tuple(value for value in self._selected_ids() if self.store.records[value].get("deleted"))
        if not ids:
            self.status.set("勾选“显示已移除”，选中需要恢复的图片。")
            return
        def task():
            with self.rotation.lock:
                count = self.store.restore_removed(ids)
            return {"restored": count}
        self._job(task, "已恢复选中图片 · 原收藏标记保留")

    def _export(self):
        if self.busy:
            return
        ids = self._selected_ids()
        if not ids:
            self.status.set("请先选择要保存的图片；只会复制选中的图片。")
            return
        if len(ids) > 500:
            self.status.set("每次最多保存 500 张，请缩小选择范围。")
            return
        records = [dict(self.store.records[value]) for value in ids]
        if len(records) > 1:
            local_count = sum(record.get("origin") == "local" for record in records)
            text = f"将仅复制选中的 {len(records)} 张图片（其中 {local_count} 张为本地图片）及作者许可清单。\n原文件保留，同名文件不会覆盖。"
            if not messagebox.askokcancel("保存选中图片", text, parent=self.root):
                return
        target = filedialog.askdirectory(parent=self.root, title=f"选择壁纸保存文件夹（仅保存选中 {len(records)} 张）", mustexist=True)
        if not target:
            return
        cancel = threading.Event()
        def task():
            result = export_records(records, Path(target), cancel_event=cancel)
            return {"export": result.get("count", 0), "errors": result.get("errors", []),
                    "cancelled": result.get("cancelled", False), "manifest": result.get("manifest_markdown", "")}
        if self._job(task, "选中图片已保存", kind="export", cancellable=True):
            self.cancel_event = cancel

    def _source(self):
        item = self.store.records.get(self.selected_id)
        if not item:
            self.status.set("请先选择一张图片。")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("图片作者、来源与许可")
        dialog.geometry("660x450")
        dialog.minsize(450, 320)
        dialog.configure(bg=COLORS["bg"])
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        box = tk.Text(frame, wrap="word", font=(FONT, 10), padx=14, pady=14,
                      bg=COLORS["surface"], fg=COLORS["ink"], insertbackground=COLORS["ink"],
                      relief="flat", highlightthickness=1, highlightbackground=COLORS["line"])
        scroll = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        box.pack(fill="both", expand=True)
        text = (f"标题：{item['title']}\n\n作者：{item.get('author', '')}\n\n许可：{item.get('license_name', '')}"
                f"\n许可链接：{item.get('license_url', '')}\n\n来源：{item.get('source_url', '') or '本地选择的图片，未上传'}"
                f"\n\n显示说明：{item.get('crop_note', '居中裁切，不修改原图')}"
                "\n\n在线图片另作分享或出版前，请按原页面核对署名、许可及可能的人格权要求。")
        box.insert("1.0", text)
        box.configure(state="disabled")
        buttons = ttk.Frame(dialog, padding=(16, 0, 16, 16))
        buttons.pack(fill="x")
        if safe_source_url(item.get("source_url", "")):
            ttk.Button(buttons, text="打开来源页", command=lambda: self._open_url(item["source_url"])).pack(side="left")
        if safe_source_url(item.get("license_url", "")):
            ttk.Button(buttons, text="查看许可", command=lambda: self._open_url(item["license_url"])).pack(side="left", padx=7)
        ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side="right")

    @staticmethod
    def _open_url(url):
        if safe_source_url(url):
            webbrowser.open(url)

    @staticmethod
    def _open_website(url):
        # Curated exact URLs are distinct from untrusted metadata links.
        if url in WEBSITE_URLS:
            webbrowser.open(url)

    def _buttons(self):
        state = "disabled" if self.busy else "normal"
        for button in self._job_buttons:
            button.configure(state=state)
        self.pause_btn.configure(text="恢复换图" if self.rotation.paused and self.rotation.enabled else "暂停换图")
        if self.busy and self.job_kind in {"download", "export"}:
            self.pause_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal" if self.busy and self.cancel_event is not None and not self.cancel_event.is_set() else "disabled")

    def _complete(self, kind, text, result):
        self.busy = False
        self.progress_bar.stop()
        self.activity.set("待机")
        self.cancel_event = None
        if kind == "error":
            # Background/tray jobs never force a hidden settings window open or
            # display repeating error dialogs. The status remains available.
            self.status.set("操作未完成，当前壁纸保留：" + text)
            self._refresh()
            return
        focus_id = None
        if isinstance(result, dict):
            if "import" in result:
                text += f" · 新增 {result['import']} 张"
            elif "download" in result:
                text = ("获取已取消" if result.get("cancelled") else text) + f" · 新增 {result['download']} 张"
                if result.get("message"):
                    text += " · " + result["message"]
            elif "export" in result:
                text = ("保存已取消" if result.get("cancelled") else text) + f" · 成功 {result['export']} 张"
                if result.get("manifest"):
                    text += " · 已附作者许可清单"
            elif "removed" in result:
                text = f"已移除 {result['removed']} 张 · 原文件与收藏保留，勾选“显示已移除”可恢复"
            elif "restored" in result:
                text = f"已恢复 {result['restored']} 张 · 原收藏标记保留"
            elif "id" in result:
                focus_id = result["id"]
            errors = result.get("errors", [])
            if errors:
                text += f" · {len(errors)} 项未完成：{str(errors[0])[:160]}"
        self.status.set(text)
        # Invalidate captions when favorite/blocked/deleted changed with the
        # same selected ID and canvas dimensions.
        self._preview_key = None
        self._refresh(focus_id=focus_id)

    def _poll(self):
        if self.closing:
            return
        try:
            while True:
                kind, text, result = self.events.get_nowait()
                if kind == "tray":
                    actions = {
                        "show": self._show,
                        "next": lambda: self._job(self.rotation.next, "已切换下一张", kind="rotation"),
                        "previous": lambda: self._job(self.rotation.previous, "已切换上一张", kind="rotation"),
                        "pause": self._pause,
                        "restore": lambda: self._job(self.rotation.restore, "已恢复原壁纸", kind="rotation"),
                        "quit": self._quit,
                    }
                    action = actions.get(text)
                    if action:
                        action()
                    if self.closing:
                        return
                elif kind == "progress":
                    if self.busy and not (self.cancel_event is not None and self.cancel_event.is_set()):
                        self.status.set(text)
                else:
                    self._complete(kind, text, result)
        except queue.Empty:
            pass
        if not self.busy:
            now = time.monotonic()
            if self.rotation.enabled and not self.rotation.paused and self.rotation.due is not None and now >= self.rotation.due:
                # An overdue rotation remains due while downloading; catch up
                # once on worker completion, then the engine schedules forward.
                self._job(lambda: self.rotation.tick(), "已按计划换图", kind="rotation", background=True)
            elif self.rotation.enabled:
                hours = self.store.settings.get("download_hours", 6)
                if hours and self.store.settings.get("mode") != "local" and now - self.last_download >= hours * 3600:
                    self._download(auto=True)
        self.root.after(300, self._poll)

    def _start_tray(self):
        try:
            import pystray
            icon = Image.new("RGB", (64, 64), COLORS["surface"])
            draw = ImageDraw.Draw(icon)
            draw.ellipse((39, 11, 49, 21), fill=COLORS["accent"])
            draw.line([(8, 51), (24, 28), (37, 45), (47, 32), (57, 51)], fill=COLORS["accent"], width=4)
            def item(label, action, default=False):
                return pystray.MenuItem(label, lambda icon, entry: self.events.put(("tray", action, None)), default=default)
            self.tray = pystray.Icon("YuliaoWallpaper", icon, "雨聊壁纸", pystray.Menu(
                item("打开图库", "show", True), item("上一张", "previous"), item("下一张", "next"),
                item("暂停 / 恢复换图", "pause"), item("恢复原壁纸", "restore"), item("退出", "quit")))
            self.tray.run_detached()
        except Exception:
            self.tray = None
            self.status.set("窗口模式可用，托盘未启动；关闭窗口将退出，当前壁纸保留。")

    def _show(self):
        self.root.deiconify()
        self.root.lift()

    def _close_window(self):
        if self.tray:
            self.root.withdraw()
        else:
            self._quit()

    def _quit(self):
        if self.busy:
            if self.cancel_event is not None:
                self._cancel()
                self.status.set("已请求取消当前任务；结束后请再点击退出，已完成图片会保留。")
            else:
                self.status.set("请等待当前本地操作结束后再退出。")
            return
        self.closing = True
        self.rotation.pause()
        if self._preview_after is not None:
            self.root.after_cancel(self._preview_after)
        if self.tray:
            self.tray.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()
