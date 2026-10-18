"""AutoCat 图形界面（tkinter）。

界面分三块：
- 顶部工具条：开始/停止录制、保存格式、录屏目录
- 中部标签页：① 录制与回放  ② 定时任务
- 底部：运行日志（所有模块的 logging 输出都会实时汇到这里）

线程模型：pynput 监听器、回放、调度器都跑在自己的线程里，任何 UI 更新一律通过
``self.queue`` 抛回主线程处理，避免 tkinter 跨线程调用崩溃。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import colorchooser, filedialog, font as tkfont, messagebox, simpledialog, ttk

from . import __version__, wininput
from .config import BLOCKED_POLICIES, OVERLAY_CORNERS, Config
from .editor import RecordingEditor
from .motion import PROFILES, get_profile
from .overlay import RecorderOverlay
from .player import Player
from .recorder import Recorder
from .scheduler import KIND_LABELS, KINDS, Scheduler, Task
from .storage import (
    FORMATS,
    describe,
    format_duration,
    load_recording,
    save_recording,
)

log = logging.getLogger("autocat.gui")

#: 窗口的逻辑尺寸（100% 缩放下的像素），实际会按屏幕 DPI 放大
WINDOW_SIZE = (1040, 700)
MIN_WINDOW_SIZE = (900, 680)

#: 界面配色（浅色、低饱和的青绿主题）
BG = "#f3f6f4"
CARD = "#ffffff"
INK = "#1f2a26"
MUTED = "#6b7a72"
ACCENT = "#1e856a"
ACCENT_DARK = "#146b55"
BORDER = "#d9e3de"
SOFT = "#e8f0ec"


def enable_dpi_awareness() -> None:
    """让进程对高 DPI 屏幕「感知」。

    不做这件事时，Windows 会把整个窗口位图拉伸到 125%/150%，字会发虚。
    必须在创建任何窗口之前调用。
    """
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Win 8.1+：系统级 DPI 感知
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()       # Win 7+ 的退路
    except Exception:
        pass


def set_app_user_model_id() -> None:
    """让任务栏把 AutoCat 当成独立应用，而不是 python.exe。"""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AutoCat.KeyboardMouseAutomation"
        )
    except Exception:
        pass



# ---------------------------------------------------------------------------
# logging -> 队列（供界面实时显示）
# ---------------------------------------------------------------------------
class QueueLogHandler(logging.Handler):
    """把日志记录塞进队列，由主线程取出显示。"""

    def __init__(self, sink: "queue.Queue") -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put(("log", (self.format(record), record.levelno), None))
        except Exception:  # pragma: no cover - 解释器关闭时可能失败
            pass


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class AutoCatGUI:
    """AutoCat 主窗口。"""

    def __init__(self, root: tk.Tk, base_dir: str) -> None:
        self.root = root
        self.base_dir = base_dir
        self.rec_dir = os.path.join(base_dir, "recordings")
        self.schedule_path = os.path.join(base_dir, "schedules.json")
        os.makedirs(self.rec_dir, exist_ok=True)

        self.queue: "queue.Queue" = queue.Queue()
        self.player: Player | None = None
        self._play_lock = threading.Lock()
        self._last_path: str | None = None
        self._tick_count = 0
        self._poll_id = None
        self._shutting_down = False
        self._log_handler: logging.Handler | None = None
        # 录制参数快照（在开始录制时于主线程写入，供监听线程安全读取）
        self._pending_fmt = "json"
        self._pending_moves = True
        self._pending_screen = ""
        self._recording_ui_active = False
        self._minimized_by_recording = False
        self._prev_window_state = "normal"

        self.config = Config(os.path.join(base_dir, "config.json"))
        profile = get_profile(self.config.get("mouse_profile", "standard"))

        self._setup_style()
        self._set_window_icon()
        self._build_menu()
        self._build_toolbar()
        self._build_log()
        self._build_notebook()

        # 录制器：监听常开，热键随时可用
        self.recorder = Recorder(
            on_stop=self._on_record_stop,
            on_start=lambda: self._post("recording", True),
            profile=profile,
            record_moves=bool(self.config.get("record_moves", True)),
        )
        self.recorder.start()

        # 录制时显示的置顶悬浮窗
        self.overlay = RecorderOverlay(
            self.root,
            self.recorder,
            self.config,
            on_stop=self.stop_recording,
            on_cancel=self.cancel_recording,
            on_rect_change=self.recorder.set_ignore_rect,
        )

        # 调度器
        self.scheduler = Scheduler(
            runner=self._run_scheduled,
            path=self.schedule_path,
            on_change=lambda: self._post("tasks"),
            on_fire=lambda task: self._post("task_fired", task),
        )
        self.scheduler.start()

        self._install_log_handler()
        self.refresh_recordings()
        self.refresh_tasks()
        self._update_state_badge()
        self.root.after(120, self._poll)
        log.info("AutoCat v%s 已就绪，录制目录：%s", __version__, self.rec_dir)
        if not wininput.current_process_elevated():
            log.info("当前为普通权限运行；若要自动化任务管理器等管理员窗口，请以管理员身份启动。")

    # ==================================================================
    # 外观
    # ==================================================================
    def _setup_style(self) -> None:
        # 先量出屏幕缩放比例，再让 Tk 按真实 DPI 换算「点」到像素
        try:
            dpi = float(self.root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        self.scale = max(1.0, round(dpi / 96.0, 3))
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

        self.root.title(f"AutoCat {__version__} — 键鼠自动化")
        width = self.px(WINDOW_SIZE[0])
        height = self.px(WINDOW_SIZE[1])
        screen_w = self.root.winfo_screenwidth()
        # 从屏幕顶部往下放，避免在 125%/150% 缩放下窗口底部被任务栏盖住
        self.root.geometry(
            f"{width}x{height}+{max(0, (screen_w - width) // 2)}+{self.px(12)}"
        )
        self.root.minsize(self.px(MIN_WINDOW_SIZE[0]), self.px(MIN_WINDOW_SIZE[1]))
        self.root.configure(bg=BG)

        style = ttk.Style()
        # clam 才能稳定地自定义背景色和扁平控件；vista 主题会忽略大部分颜色设置
        if "clam" in style.theme_names():
            style.theme_use("clam")

        # 中文界面用雅黑更清楚
        try:
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=9)
        except Exception:
            pass

        style.configure(".", background=BG, foreground=INK, font=("Microsoft YaHei UI", 9))
        style.configure("TFrame", background=BG)
        style.configure("Toolbar.TFrame", background=BG)
        style.configure("Status.TFrame", background=SOFT)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure(
            "Title.TLabel", font=("Microsoft YaHei UI", 10, "bold"), foreground=INK,
        )
        style.configure("Hint.TLabel", foreground=MUTED, background=BG)
        style.configure("Muted.TLabel", foreground=MUTED, background=BG)
        style.configure(
            "Badge.TLabel", font=("Microsoft YaHei UI", 10, "bold"),
            background=SOFT, padding=(self.px(10), self.px(3)), anchor="center",
        )
        style.configure("Status.TLabel", foreground=MUTED, background=SOFT,
                        padding=(self.px(10), self.px(4)))

        style.configure(
            "TButton",
            background=SOFT, foreground=INK, borderwidth=0, relief="flat",
            padding=(self.px(11), self.px(5)),
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "TButton",
            background=[("disabled", "#eef2f0"), ("pressed", "#cfded8"), ("active", "#dce9e4")],
            foreground=[("disabled", "#9aa7a0")],
        )
        style.configure(
            "Accent.TButton", background=ACCENT, foreground="#ffffff",
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(self.px(13), self.px(6)),
        )
        style.map(
            "Accent.TButton",
            background=[("disabled", "#a9c8bd"), ("pressed", ACCENT_DARK), ("active", "#25987a")],
            foreground=[("disabled", "#f2f7f5")],
        )
        style.configure(
            "Danger.TButton", background="#f6e4e4", foreground="#a52525",
            font=("Microsoft YaHei UI", 9),
        )
        style.map("Danger.TButton", background=[("active", "#efd3d3")])

        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
        style.configure(
            "TNotebook.Tab", background=SOFT, foreground=MUTED,
            padding=(self.px(18), self.px(7)),
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD)],
            foreground=[("selected", INK)],
            expand=[("selected", (0, 0, 0, 0))],
        )
        style.configure(
            "Treeview", background=CARD, fieldbackground=CARD, foreground=INK,
            rowheight=self.px(26), borderwidth=0, relief="flat",
        )
        style.configure(
            "Treeview.Heading", background=SOFT, foreground=MUTED,
            font=("Microsoft YaHei UI", 9, "bold"), relief="flat", padding=(self.px(4), self.px(5)),
        )
        style.map(
            "Treeview",
            background=[("selected", "#cdeadf")],
            foreground=[("selected", INK)],
        )
        style.map("Treeview.Heading", background=[("active", "#e0eae5")])
        style.configure(
            "TLabelframe", background=BG, bordercolor=BORDER,
            relief="solid", borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label", background=BG, foreground=MUTED,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "TCheckbutton", background=BG, foreground=INK,
            indicatorbackground=CARD, indicatorforeground=ACCENT,
            indicatorsize=self.px(13), indicatormargin=(0, 0, self.px(6), 0),
        )
        style.configure("TEntry", fieldbackground=CARD, foreground=INK, padding=self.px(4),
                        borderwidth=1, relief="solid")
        style.configure("TCombobox", fieldbackground=CARD, foreground=INK, padding=self.px(3))
        style.configure("TSpinbox", fieldbackground=CARD, foreground=INK, padding=self.px(3))
        style.configure("TSeparator", background=BORDER)
        style.configure("TScrollbar", background=SOFT, troughcolor=BG, borderwidth=0)

    def px(self, value: float) -> int:
        """把逻辑像素换算成当前屏幕的实际像素。"""
        return max(1, int(round(value * getattr(self, "scale", 1.0))))

    def _set_window_icon(self) -> None:
        icon = os.path.join(self.base_dir, "assets", "autocat.ico")
        if not os.path.exists(icon):
            return
        try:
            self.root.iconbitmap(default=icon)
        except Exception:
            log.debug("设置窗口图标失败", exc_info=True)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="打开录制目录", command=self.open_rec_dir)
        file_menu.add_command(label="切换录制目录…", command=self.choose_rec_dir)
        file_menu.add_separator()
        file_menu.add_command(label="刷新列表", command=self.refresh_recordings)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="立即播放选中录制", command=self.play_selected)
        tools_menu.add_command(label="停止回放", command=self.stop_playback)
        tools_menu.add_command(label="编辑录制内容", command=self.open_editor)
        tools_menu.add_separator()
        tools_menu.add_command(label="设置…", command=self.open_settings)
        tools_menu.add_command(label="以管理员身份重启…", command=self.restart_as_admin)
        tools_menu.add_separator()
        tools_menu.add_command(label="清空日志", command=lambda: self.log_text.delete("1.0", "end"))
        menubar.add_cascade(label="工具", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="快捷键说明", command=self.show_help)
        help_menu.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.root.config(menu=menubar)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(12, 10, 12, 6), style="Toolbar.TFrame")
        bar.pack(fill="x")

        self.btn_record = ttk.Button(
            bar, text="● 开始录制", command=self.start_recording, style="Accent.TButton"
        )
        self.btn_record.pack(side="left")

        self.btn_stop = ttk.Button(
            bar, text="■ 停止并保存", command=self.stop_recording, state="disabled"
        )
        self.btn_stop.pack(side="left", padx=(8, 0))

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=14, pady=2)

        self.var_moves = tk.BooleanVar(value=bool(self.config.get("record_moves", True)))
        tk.Checkbutton(
            bar, text="记录鼠标移动", variable=self.var_moves, command=self._on_moves_toggle,
            bg=BG, fg=INK, activebackground=BG, activeforeground=INK,
            selectcolor=CARD, font=("Microsoft YaHei UI", 9),
            bd=0, highlightthickness=0, cursor="hand2",
        ).pack(side="left")

        ttk.Label(bar, text="精度").pack(side="left", padx=(14, 5))
        self.var_profile = tk.StringVar(
            value=get_profile(self.config.get("mouse_profile", "standard")).label
        )
        self.cmb_profile = ttk.Combobox(
            bar, textvariable=self.var_profile, width=6, state="readonly",
            values=[p.label for p in PROFILES.values()],
        )
        self.cmb_profile.pack(side="left")
        self.cmb_profile.bind("<<ComboboxSelected>>", lambda _e: self._on_profile_change())

        ttk.Label(bar, text="格式").pack(side="left", padx=(14, 5))
        self.var_format = tk.StringVar(value="json")
        ttk.Combobox(
            bar, textvariable=self.var_format, values=list(FORMATS),
            width=6, state="readonly",
        ).pack(side="left")

        self.lbl_badge = ttk.Label(bar, text="空闲", style="Badge.TLabel", foreground="#1f6b4f")
        self.lbl_badge.pack(side="right")

    def _build_notebook(self) -> None:
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(2, 4))

        self.tab_record = ttk.Frame(self.nb, padding=10)
        self.tab_sched = ttk.Frame(self.nb, padding=10)
        self.tab_editor = ttk.Frame(self.nb, padding=0)
        self.nb.add(self.tab_record, text="  录制与回放  ")
        self.nb.add(self.tab_sched, text="  定时任务  ")
        self.nb.add(self.tab_editor, text="  录制内容  ")

        self._build_record_tab()
        self._build_sched_tab()

        self.editor = RecordingEditor(self.tab_editor, on_saved=self._on_editor_saved)
        self.editor.pack(fill="both", expand=True)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ------------------------------------------------------------------
    # 标签页 1：录制与回放
    # ------------------------------------------------------------------
    def _build_record_tab(self) -> None:
        top = ttk.Frame(self.tab_record)
        top.pack(fill="both", expand=True)

        header = ttk.Frame(top)
        header.pack(fill="x")
        ttk.Label(header, text="录制文件", style="Title.TLabel").pack(side="left")
        self.lbl_detail = ttk.Label(header, text="未选择文件", style="Hint.TLabel")
        self.lbl_detail.pack(side="right")

        wrap = ttk.Frame(top)
        wrap.pack(fill="both", expand=True, pady=(6, 8))

        columns = ("count", "duration", "created", "format", "size")
        self.tree = ttk.Treeview(wrap, columns=columns, show="tree headings", height=8)
        self.tree.heading("#0", text="文件名")
        self.tree.heading("count", text="事件数")
        self.tree.heading("duration", text="时长")
        self.tree.heading("created", text="录制时间")
        self.tree.heading("format", text="格式")
        self.tree.heading("size", text="大小")
        self.tree.column("#0", width=self.px(300), anchor="w")
        for col, width in (("count", 70), ("duration", 70), ("created", 150), ("format", 70), ("size", 80)):
            self.tree.column(col, width=self.px(width), anchor="center")
        self.tree.tag_configure("odd", background="#eef5f1")
        self.tree.tag_configure("even", background=CARD)

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.lbl_empty = ttk.Label(
            wrap,
            text="还没有录制文件\n按 F9 开始录制，F10 停止并保存",
            style="Hint.TLabel", justify="center",
        )
        self.lbl_empty.place(relx=0.5, rely=0.42, anchor="center")

        self.tree.bind("<<TreeviewSelect>>", self._on_recording_select)
        self.tree.bind("<Double-1>", lambda _e: self.play_selected())

        row = ttk.Frame(top)
        row.pack(fill="x")
        ttk.Button(row, text="刷新", command=self.refresh_recordings).pack(side="left")
        ttk.Button(row, text="导入…", command=self.import_recording).pack(side="left", padx=6)
        ttk.Button(row, text="编辑内容", command=self.open_editor).pack(side="left")
        ttk.Button(row, text="重命名…", command=self.rename_recording).pack(side="left", padx=6)
        ttk.Button(row, text="删除", command=self.delete_recording, style="Danger.TButton").pack(
            side="left"
        )
        ttk.Button(row, text="打开目录", command=self.open_rec_dir).pack(side="left", padx=6)

        # ---- 回放设置：设置一行、操作一行，窄窗口下也不会挤爆 ----
        play = ttk.LabelFrame(top, text="回放设置", padding=(12, 10))
        play.pack(fill="x", pady=(10, 0))

        ttk.Label(play, text="速度").grid(row=0, column=0, sticky="w")
        self.var_speed = tk.StringVar(value="1.0")
        ttk.Combobox(
            play, textvariable=self.var_speed, width=6,
            values=("0.25", "0.5", "0.75", "1.0", "1.5", "2.0", "3.0", "5.0"),
        ).grid(row=0, column=1, padx=(5, 18))

        ttk.Label(play, text="循环次数").grid(row=0, column=2, sticky="w")
        self.var_loops = tk.IntVar(value=1)
        self.spin_loops = ttk.Spinbox(play, from_=1, to=9999, width=8, textvariable=self.var_loops)
        self.spin_loops.grid(row=0, column=3, padx=(5, 8))
        self.var_infinite = tk.BooleanVar(value=False)
        tk.Checkbutton(
            play, text="无限循环", variable=self.var_infinite, command=self._on_infinite_toggle,
            bg=BG, fg=INK, activebackground=BG, activeforeground=INK,
            selectcolor=CARD, font=("Microsoft YaHei UI", 9),
            bd=0, highlightthickness=0, cursor="hand2",
        ).grid(row=0, column=4, padx=(0, 18))

        ttk.Label(play, text="开始前延时(秒)").grid(row=0, column=5, sticky="w")
        self.var_delay = tk.DoubleVar(value=1.0)
        ttk.Spinbox(play, from_=0, to=600, increment=0.5, width=8, textvariable=self.var_delay).grid(
            row=0, column=6, padx=(5, 12)
        )

        self.btn_play = ttk.Button(
            play, text="▶ 立即播放", command=self.play_selected, style="Accent.TButton"
        )
        self.btn_play.grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.btn_abort = ttk.Button(
            play, text="■ 停止回放", command=self.stop_playback, state="disabled"
        )
        self.btn_abort.grid(row=1, column=2, sticky="w", pady=(12, 0))

        ttk.Label(
            play,
            text="F12 可中止 · 播放前请先切到目标窗口，延时就是留给你切窗口的时间。",
            style="Hint.TLabel",
        ).grid(row=1, column=3, columnspan=4, sticky="w", padx=(8, 0), pady=(12, 0))

    # ------------------------------------------------------------------
    # 标签页 2：定时任务
    # ------------------------------------------------------------------
    def _build_sched_tab(self) -> None:
        top = ttk.LabelFrame(self.tab_sched, text="任务列表", padding=10)
        top.pack(fill="both", expand=True)

        columns = ("file", "rule", "next", "runs", "status")
        self.task_tree = ttk.Treeview(top, columns=columns, show="tree headings", height=6)
        self.task_tree.heading("#0", text="任务名称")
        self.task_tree.heading("file", text="录制文件")
        self.task_tree.heading("rule", text="触发规则")
        self.task_tree.heading("next", text="下次执行")
        self.task_tree.heading("runs", text="已执行")
        self.task_tree.heading("status", text="状态")
        self.task_tree.column("#0", width=self.px(170), anchor="w")
        for col, width in (("file", 200), ("rule", 190), ("next", 165), ("runs", 70), ("status", 70)):
            self.task_tree.column(col, width=self.px(width), anchor="center")
        self.task_tree.tag_configure("odd", background="#eef5f1")
        self.task_tree.tag_configure("even", background=CARD)

        vsb = ttk.Scrollbar(top, orient="vertical", command=self.task_tree.yview)
        self.task_tree.configure(yscrollcommand=vsb.set)
        self.task_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.task_tree.bind("<<TreeviewSelect>>", self._on_task_select)
        self.lbl_task_empty = ttk.Label(
            top, text="还没有定时任务\n在下方填写后点「＋ 添加任务」",
            style="Hint.TLabel", justify="center",
        )
        self.lbl_task_empty.place(relx=0.5, rely=0.4, anchor="center")

        form = ttk.LabelFrame(self.tab_sched, text="新建 / 编辑任务", padding=12)
        form.pack(fill="x", pady=(10, 0))

        ttk.Label(form, text="任务名称").grid(row=0, column=0, sticky="w", pady=3)
        self.var_task_name = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_task_name, width=24).grid(row=0, column=1, sticky="w", padx=(4, 16))

        ttk.Label(form, text="录制文件").grid(row=0, column=2, sticky="w")
        self.var_task_file = tk.StringVar()
        self.cmb_task_file = ttk.Combobox(form, textvariable=self.var_task_file, width=36)
        self.cmb_task_file.grid(row=0, column=3, columnspan=3, sticky="w", padx=(4, 16))

        ttk.Label(form, text="触发方式").grid(row=1, column=0, sticky="w", pady=3)
        self.var_kind = tk.StringVar(value=KIND_LABELS["interval"])
        cmb_kind = ttk.Combobox(
            form, textvariable=self.var_kind, width=22, state="readonly",
            values=[KIND_LABELS[k] for k in KINDS],
        )
        cmb_kind.grid(row=1, column=1, sticky="w", padx=(4, 16))
        cmb_kind.bind("<<ComboboxSelected>>", lambda _e: self._on_kind_change())

        # 动态参数区
        self.param_frame = ttk.Frame(form)
        self.param_frame.grid(row=1, column=2, columnspan=4, sticky="w")

        self.lbl_at = ttk.Label(self.param_frame, text="时刻")
        self.var_at = tk.StringVar(
            value=(datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        )
        self.w_at = ttk.Entry(self.param_frame, textvariable=self.var_at, width=22)

        self.lbl_seconds = ttk.Label(self.param_frame, text="秒数")
        self.var_seconds = tk.IntVar(value=60)
        self.w_seconds = ttk.Spinbox(
            self.param_frame, from_=1, to=86400, width=10, textvariable=self.var_seconds
        )

        self.lbl_tod = ttk.Label(self.param_frame, text="时间 HH:MM")
        self.var_tod = tk.StringVar(value="09:00")
        self.w_tod = ttk.Entry(self.param_frame, textvariable=self.var_tod, width=10)

        ttk.Label(form, text="播放速度").grid(row=2, column=0, sticky="w", pady=3)
        self.var_task_speed = tk.StringVar(value="1.0")
        ttk.Combobox(
            form, textvariable=self.var_task_speed, width=8,
            values=("0.5", "1.0", "1.5", "2.0", "3.0"),
        ).grid(row=2, column=1, sticky="w", padx=(4, 16))

        ttk.Label(form, text="循环次数").grid(row=2, column=2, sticky="w")
        self.var_task_loops = tk.IntVar(value=1)
        ttk.Spinbox(form, from_=1, to=9999, width=8, textvariable=self.var_task_loops).grid(
            row=2, column=3, sticky="w", padx=(4, 16)
        )

        row = ttk.Frame(form)
        row.grid(row=3, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Button(row, text="＋ 添加任务", command=self.add_task, style="Accent.TButton").pack(side="left")
        ttk.Button(row, text="更新选中", command=self.update_task).pack(side="left", padx=6)
        ttk.Button(row, text="启用 / 停用", command=self.toggle_task).pack(side="left")
        ttk.Button(row, text="立即执行", command=self.run_task_now).pack(side="left", padx=6)
        ttk.Button(
            row, text="删除选中", command=self.delete_task, style="Danger.TButton"
        ).pack(side="left", padx=(6, 0))

        self.lbl_sched = ttk.Label(form, text="调度器运行中", style="Hint.TLabel")
        self.lbl_sched.grid(row=4, column=0, columnspan=6, sticky="e", pady=(8, 0))

        self._on_kind_change()

    def _build_log(self) -> None:
        # 状态栏和日志先于 Notebook 打包，保证窗口再矮也不会把日志挤成一条线
        status = ttk.Frame(self.root, style="Status.TFrame")
        status.pack(side="bottom", fill="x")
        self.status_frame = status
        self.var_status = tk.StringVar()
        ttk.Label(status, textvariable=self.var_status, style="Status.TLabel").pack(side="left")
        ttk.Label(
            status, text="F9 开始 · F10 停止 · F12 中止",
            style="Status.TLabel",
        ).pack(side="right")
        self._update_status_bar()

        frame = ttk.LabelFrame(self.root, text="运行日志", padding=(10, 6, 10, 10))
        frame.pack(side="bottom", fill="x", padx=12, pady=(0, 6))

        self.log_text = tk.Text(
            frame, height=6, wrap="none", state="disabled", font=("Consolas", 9),
            bg=CARD, fg=INK, relief="flat", padx=6, pady=4,
            insertbackground=INK, selectbackground="#cdeadf",
        )
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.log_text.tag_configure("WARNING", foreground="#b35c00")
        self.log_text.tag_configure("ERROR", foreground="#b3261e")
        self.log_text.tag_configure("DEBUG", foreground="#8a968f")
        self.log_text.tag_configure("INFO", foreground=INK)

    def _update_status_bar(self) -> None:
        level = "管理员" if wininput.current_process_elevated() else "普通权限"
        self.var_status.set(
            f"AutoCat v{__version__}   ·   录制目录：{self.rec_dir}   ·   {level}"
        )

    # ==================================================================
    # 线程 <-> 主线程
    # ==================================================================
    def _post(self, kind: str, payload=None, level=None) -> None:
        self.queue.put((kind, payload, level))

    def _poll(self) -> None:
        """主线程轮询队列：日志、状态、任务刷新都在这里落地。"""
        if self._shutting_down:
            return
        try:
            while True:
                kind, payload, level = self.queue.get_nowait()
                if kind == "log":
                    self._append_log(payload[0], payload[1])
                elif kind == "tasks":
                    self.refresh_tasks()
                elif kind == "recording":
                    self._on_recording_state(bool(payload))
                elif kind == "record_saved":
                    self.refresh_recordings(select=payload)
                elif kind == "play_state":
                    self._on_play_state(payload[0], payload[1])
                elif kind == "task_fired":
                    self._append_log(f"[定时] 触发「{payload.name}」", logging.INFO)
        except queue.Empty:
            pass

        self._tick_count += 1
        if self._tick_count % 8 == 0:  # 约 1 秒刷新一次倒计时
            self.refresh_tasks()

        self._poll_id = self.root.after(120, self._poll)

    def _append_log(self, text: str, levelno: int = logging.INFO) -> None:
        tag = logging.getLevelName(levelno)
        if tag not in ("INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL"):
            tag = "INFO"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", tag)
        self.log_text.see("end")
        # 防止日志无限增长
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.configure(state="disabled")

    def _install_log_handler(self) -> None:
        handler = QueueLogHandler(self.queue)
        handler.setLevel(logging.DEBUG if "--debug" in sys.argv else logging.INFO)
        root_logger = logging.getLogger("autocat")
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)
        self._log_handler = handler

    # ==================================================================
    # 录制
    # ==================================================================
    def start_recording(self) -> None:
        if self.player is not None and self.player.is_playing:
            messagebox.showwarning("正在回放", "回放期间不能录制，请先停止回放。")
            return
        self.recorder.record_moves = bool(self.var_moves.get())
        self.recorder.profile = get_profile(self.var_profile.get())
        # 在录制开始（主线程）就把设置记下来：停止回调跑在监听线程，
        # 那里读 tkinter 变量是不安全的。
        self._pending_fmt = self.var_format.get() or "json"
        self._pending_moves = bool(self.var_moves.get())
        self._pending_screen = self._screen_size()
        if self.recorder.start_recording():
            self._update_state_badge(recording=True)

    def stop_recording(self) -> None:
        self.recorder.stop_recording()

    def cancel_recording(self) -> None:
        self.recorder.cancel_recording()
        self.overlay.hide()
        self.recorder.set_ignore_rect(None)
        self._on_recording_state(False)

    def _on_recording_state(self, recording: bool) -> None:
        """录制开始 / 结束后统一收拾界面：最小化、悬浮窗、状态徽标。"""
        self._update_state_badge(recording=recording)
        if recording:
            if self._recording_ui_active:
                return
            self._recording_ui_active = True
            # 悬浮窗不能盖在编辑器上干扰操作，录制时把主窗口最小化
            try:
                # Tk 在 Windows 上最大化时 state 是 "zoomed"，不是 "normal"
                if self.root.state() not in ("iconic", "withdrawn"):
                    self._prev_window_state = self.root.state()
                    self._minimized_by_recording = True
                    self.root.iconify()
            except Exception:
                pass
            if self.config.get("overlay.enabled", True):
                # 等主窗口真正最小化后再显示，避免被一起收起
                self.root.after(180, self._show_overlay)
            return

        if not self._recording_ui_active:
            return
        self._recording_ui_active = False
        self.overlay.hide()
        self.recorder.set_ignore_rect(None)
        if self._minimized_by_recording:
            self._minimized_by_recording = False
            try:
                self.root.deiconify()
                if self._prev_window_state == "zoomed":
                    self.root.state("zoomed")
                self.root.lift()
            except Exception:
                pass

    def _show_overlay(self) -> None:
        if self._shutting_down or not self.recorder.recording:
            return
        if not self.config.get("overlay.enabled", True):
            return
        # 主窗口被完全隐藏（例如自动化测试）时不开悬浮窗，避免干扰
        try:
            if self.root.state() == "withdrawn":
                return
        except Exception:
            pass
        try:
            self.overlay.show()
        except Exception:
            log.exception("显示录制悬浮窗失败")

    def _on_profile_change(self) -> None:
        profile = get_profile(self.var_profile.get())
        self.recorder.profile = profile
        self.recorder.move_threshold = profile.min_distance
        self.config.set("mouse_profile", profile.key)
        log.info("鼠标记录档位：%s（%s）", profile.label, profile.description)

    def _on_moves_toggle(self) -> None:
        enabled = bool(self.var_moves.get())
        self.recorder.record_moves = enabled
        self.config.set("record_moves", enabled)

    # ==================================================================
    # 录制文件列表
    # ==================================================================

    def _on_record_stop(self, events) -> None:
        """录制结束（可能来自热键线程）。只做纯 Python 的事，不碰 tkinter。"""
        if not events:
            log.warning("没有录到任何事件，未保存")
            self._post("recording", False)
            return

        fmt = self._pending_fmt
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.rec_dir, f"rec_{stamp}.{fmt}")
        meta = {
            "source": "gui",
            "events": len(events),
            "screen": self._pending_screen,
            "record_moves": self._pending_moves,
        }
        try:
            save_recording(path, events, meta=meta, fmt=fmt)
        except Exception:
            log.exception("保存录制文件失败")
            self._post("recording", False)
            return

        self._last_path = path
        self._post("recording", False)
        self._post("record_saved", path)

    def _screen_size(self) -> str:
        try:
            return f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}"
        except Exception:
            return ""

    # ==================================================================
    # 录制文件列表
    # ==================================================================
    def refresh_recordings(self, select: str | None = None) -> None:
        self.tree.delete(*self.tree.get_children())
        try:
            names = sorted(os.listdir(self.rec_dir))
        except OSError:
            names = []

        row_index = 0
        for name in names:
            path = os.path.join(self.rec_dir, name)
            if not os.path.isfile(path) or os.path.splitext(name)[1].lower() not in (".json", ".xml"):
                continue
            try:
                info = describe(path)
            except Exception as exc:
                log.warning("读取失败 %s: %s", name, exc)
                continue
            self.tree.insert(
                "", "end", iid=path, text=name,
                values=(
                    info["count"],
                    format_duration(info["duration"]),
                    info["created_at"].replace("T", " "),
                    info["format"].upper(),
                    f"{info['size'] / 1024:.1f} KB",
                ),
                tags=("odd" if row_index % 2 else "even",),
            )
            row_index += 1

        if row_index:
            self.lbl_empty.place_forget()
        else:
            self.lbl_empty.place(relx=0.5, rely=0.42, anchor="center")

        if select and self.tree.exists(select):
            self.tree.selection_set(select)
            self.tree.see(select)
        elif self._last_path and self.tree.exists(self._last_path):
            self.tree.selection_set(self._last_path)

        self.cmb_task_file.configure(values=[self.tree.item(i, "text") for i in self.tree.get_children()])
        self._on_recording_select()

    def _selected_recording(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _on_recording_select(self, _event=None) -> None:
        path = self._selected_recording()
        if not path or not os.path.exists(path):
            self.lbl_detail.configure(text="未选择文件")
            return
        try:
            payload = load_recording(path)
        except Exception as exc:
            self.lbl_detail.configure(text=f"读取失败：{exc}")
            return
        events = payload["events"]
        keys = sum(1 for e in events if str(e.get("type", "")).startswith("key"))
        clicks = sum(1 for e in events if e.get("type") == "mouse_click")
        self.lbl_detail.configure(
            text=f"{len(events)} 事件 · 键盘 {keys} · 点击 {clicks} · "
                 f"{format_duration(describe(path)['duration'])}"
        )
        # 停在「录制内容」标签页时，切换文件跟着刷新编辑器
        try:
            current = self.nb.select()
        except Exception:
            current = ""
        if current == str(self.tab_editor) and not self.editor.dirty:
            if self.editor.path != path:
                self.editor.load_path(path)

    # ==================================================================
    # 录制内容编辑器
    # ==================================================================
    def open_editor(self, path: str | None = None) -> None:
        path = path or self._selected_recording()
        if not path:
            messagebox.showinfo("编辑录制内容", "请先在列表里选择一个录制文件。")
            return
        if not os.path.exists(path):
            messagebox.showerror("文件不存在", path)
            return
        if self.editor.load_path(path):
            self.nb.select(self.tab_editor)
            self.editor.tree.focus_set()

    def _on_tab_changed(self, _event=None) -> None:
        if self.nb.select() != str(self.tab_editor):
            return
        path = self._selected_recording()
        if not path or self.editor.dirty:
            return
        if self.editor.path != path:
            self.editor.load_path(path)

    def _on_editor_saved(self) -> None:
        self.refresh_recordings(select=self.editor.path)
        self._append_log(f"[编辑] 已保存 {os.path.basename(self.editor.path or '')}", logging.INFO)

    def import_recording(self) -> None:
        paths = filedialog.askopenfilenames(
            title="导入录制文件",
            filetypes=[("AutoCat 录制", "*.json *.xml"), ("所有文件", "*.*")],
            initialdir=self.rec_dir,
        )
        for src in paths:
            try:
                payload = load_recording(src)
                dst = os.path.join(self.rec_dir, os.path.basename(src))
                save_recording(dst, payload)
                log.info("已导入 %s", os.path.basename(src))
            except Exception:
                log.exception("导入失败: %s", src)
        self.refresh_recordings()

    def rename_recording(self) -> None:
        path = self._selected_recording()
        if not path:
            messagebox.showinfo("重命名", "请先选中一个录制文件。")
            return
        old = os.path.basename(path)
        new = simpledialog.askstring("重命名", "新文件名（不含扩展名）：", initialvalue=os.path.splitext(old)[0])
        if not new:
            return
        ext = os.path.splitext(old)[1]
        target = os.path.join(self.rec_dir, f"{new}{ext}")
        try:
            os.replace(path, target)
            log.info("已重命名 %s -> %s", old, os.path.basename(target))
        except Exception:
            log.exception("重命名失败")
        self.refresh_recordings(select=target)

    def delete_recording(self) -> None:
        path = self._selected_recording()
        if not path:
            messagebox.showinfo("删除", "请先选中一个录制文件。")
            return
        if not messagebox.askyesno("删除", f"确定删除 {os.path.basename(path)} 吗？"):
            return
        try:
            os.remove(path)
            log.info("已删除 %s", os.path.basename(path))
        except Exception:
            log.exception("删除失败")
        self.refresh_recordings()

    def open_rec_dir(self) -> None:
        try:
            os.startfile(self.rec_dir)  # noqa: S606 - Windows 专用
        except Exception:
            log.exception("打开目录失败")

    def choose_rec_dir(self) -> None:
        chosen = filedialog.askdirectory(title="选择录制目录", initialdir=self.rec_dir)
        if chosen:
            self.rec_dir = chosen
            log.info("录制目录已切换为 %s", chosen)
            self.refresh_recordings()
            self._update_status_bar()

    # ==================================================================
    # 回放
    # ==================================================================
    def _playback_options(self) -> dict:
        try:
            speed = float(self.var_speed.get())
        except ValueError:
            speed = 1.0
        loops = 0 if self.var_infinite.get() else max(1, int(self.var_loops.get() or 1))
        try:
            delay = max(0.0, float(self.var_delay.get()))
        except (ValueError, tk.TclError):
            delay = 0.0
        return {"speed": speed, "loops": loops, "start_delay": delay}

    def _on_infinite_toggle(self) -> None:
        self.spin_loops.configure(state="disabled" if self.var_infinite.get() else "normal")

    def play_selected(self) -> None:
        path = self._selected_recording()
        if not path:
            messagebox.showinfo("回放", "请先在列表里选择一个录制文件。")
            return
        self._play_file(path, **self._playback_options())

    def _play_file(self, path: str, speed: float = 1.0, loops: int = 1, start_delay: float = 1.0) -> bool:
        """加载并回放一个文件。返回是否成功启动。"""
        if not self._play_lock.acquire(blocking=False):
            log.warning("已有回放在进行中，忽略本次请求：%s", os.path.basename(path))
            return False

        try:
            payload = load_recording(path)
            events = payload["events"]
        except Exception:
            log.exception("读取录制文件失败: %s", path)
            self._play_lock.release()
            return False

        if not events:
            log.warning("文件里没有事件：%s", os.path.basename(path))
            self._play_lock.release()
            return False

        self.recorder.hotkeys_enabled = False  # 回放期间别让 F9 触发录制
        self.player = Player(
            speed=speed,
            loops=loops,
            start_delay=start_delay,
            on_state=lambda state, info: self._post("play_state", (state, info)),
            on_input_blocked=str(self.config.get("playback.on_input_blocked", "stop")),
        )
        # 必须在 play_async 之前投递：回放很快时（例如干跑），Player 线程可能
        # 立刻发出 finished，晚投的 started 会把它盖掉，按钮就卡在禁用状态。
        self._post("play_state", ("started", {"total": len(events), "name": os.path.basename(path)}))
        self.player.play_async(events)
        return True

    def stop_playback(self) -> None:
        if self.player is not None and self.player.is_playing:
            self.player.abort()
        else:
            log.info("当前没有回放在进行")

    def _on_play_state(self, state: str, info: dict) -> None:
        if state == "started":
            self.btn_play.configure(state="disabled")
            self.btn_record.configure(state="disabled")
            self.btn_abort.configure(state="normal")
            self._update_state_badge(playing=True)
        else:
            self.btn_play.configure(state="normal")
            self.btn_record.configure(state="normal")
            self.btn_abort.configure(state="disabled")
            self.recorder.hotkeys_enabled = True
            if self._play_lock.locked():
                try:
                    self._play_lock.release()
                except RuntimeError:
                    pass
            self.player = None
            self._update_state_badge()
            if state == "aborted" and info:
                log.info("回放中止：已播放 %s 条", info.get("played"))
            elif state == "blocked":
                reason = str(info.get("reason") or "系统拒绝了键鼠输入注入。")
                log.error("回放被系统拦截：%s", reason)
                self._append_log(f"[回放] {reason}", logging.ERROR)
                self.root.after(20, lambda: self._offer_admin_restart(reason))

    def _offer_admin_restart(self, reason: str) -> None:
        """输入被 UIPI 拦截时给出可执行的补救办法。"""
        if messagebox.askyesno(
            "输入被系统拦截",
            f"{reason}\n\n"
            "说明：以管理员权限运行的窗口（例如任务管理器）会拒绝普通权限进程"
            "发送的按键和点击；鼠标移动不受影响，所以看起来像「鼠标能动能点不了」。\n\n"
            "是否以管理员身份重新启动 AutoCat？\n"
            "（重启后重新打开 AutoCat，再点一次播放即可）",
        ):
            if wininput.relaunch_as_admin():
                log.info("已请求以管理员身份重启")
                self.shutdown()
                try:
                    self.root.destroy()
                except Exception:
                    pass
            else:
                messagebox.showerror(
                    "重启失败",
                    "以管理员身份重启失败（UAC 被取消或系统拒绝）。\n"
                    "也可以手动右键 AutoCat →「以管理员身份运行」。",
                )

    # ==================================================================
    # 定时任务
    # ==================================================================
    def _kind_from_label(self) -> str:
        label = self.var_kind.get()
        for kind, text in KIND_LABELS.items():
            if text == label:
                return kind
        return "interval"

    def _on_kind_change(self) -> None:
        """按触发方式显示对应的参数输入框。"""
        for widget in (self.lbl_at, self.w_at, self.lbl_seconds, self.w_seconds,
                       self.lbl_tod, self.w_tod):
            widget.grid_remove()

        kind = self._kind_from_label()
        if kind == "once":
            self.lbl_at.grid(row=0, column=0, sticky="w")
            self.w_at.grid(row=0, column=1, sticky="w")
        elif kind in ("delay", "interval"):
            self.lbl_seconds.grid(row=0, column=0, sticky="w")
            self.w_seconds.grid(row=0, column=1, sticky="w")
        elif kind == "daily":
            self.lbl_tod.grid(row=0, column=0, sticky="w")
            self.w_tod.grid(row=0, column=1, sticky="w")

    def refresh_tasks(self) -> None:
        """刷新任务列表。

        行集合没变时原地更新（每秒刷倒计时用），否则整表重建；
        两种情况都会保留用户当前选中的行，避免刷新把选中弄丢。
        """
        tasks = self.scheduler.list_tasks()
        selected = self.task_tree.selection()
        existing = self.task_tree.get_children()
        wanted = tuple(task.id for task in tasks)

        if existing == wanted:
            for task in tasks:
                self.task_tree.item(task.id, values=self._task_row(task))
        else:
            self.task_tree.delete(*existing)
            for index, task in enumerate(tasks):
                self.task_tree.insert(
                    "", "end", iid=task.id, text=task.name,
                    values=self._task_row(task),
                    tags=("odd" if index % 2 else "even",),
                )

        if wanted:
            self.lbl_task_empty.place_forget()
        else:
            self.lbl_task_empty.place(relx=0.5, rely=0.4, anchor="center")

        for iid in selected:
            if self.task_tree.exists(iid):
                self.task_tree.selection_set(iid)

        self.lbl_sched.configure(
            text=f"调度器{'运行中' if self.scheduler.running else '已停止'} · 共 {len(tasks)} 条任务"
        )

    def _task_row(self, task: Task) -> tuple:
        busy = self.scheduler.busy_task == task.id
        status = "执行中" if busy else ("启用" if task.enabled else "停用")
        return (
            os.path.basename(task.file) or "—",
            task.describe(),
            (task.next_run or "—").replace("T", " "),
            task.run_count,
            status,
        )

    def _on_task_select(self, _event=None) -> None:
        selection = self.task_tree.selection()
        if not selection:
            return
        task = self.scheduler.get(selection[0])
        if not task:
            return
        self.var_task_name.set(task.name)
        self.var_task_file.set(os.path.basename(task.file))
        self.var_kind.set(KIND_LABELS.get(task.kind, KIND_LABELS["interval"]))
        self.var_at.set(task.at or self.var_at.get())
        self.var_seconds.set(int(task.seconds or 60))
        self.var_tod.set(task.time_of_day or "09:00")
        self.var_task_speed.set(str(task.speed))
        self.var_task_loops.set(int(task.loops or 1))
        self._on_kind_change()

    def _task_file_path(self) -> str:
        raw = self.var_task_file.get().strip()
        if not raw:
            return ""
        if os.path.isabs(raw) and os.path.exists(raw):
            return raw
        return os.path.join(self.rec_dir, os.path.basename(raw))

    def _collect_task_fields(self) -> dict:
        name = self.var_task_name.get().strip() or f"任务 {datetime.now().strftime('%H%M%S')}"
        path = self._task_file_path()
        kind = self._kind_from_label()
        try:
            speed = float(self.var_task_speed.get())
        except ValueError:
            speed = 1.0
        try:
            loops = max(1, int(self.var_task_loops.get() or 1))
        except (ValueError, tk.TclError):
            loops = 1
        return {
            "name": name,
            "file": path,
            "kind": kind,
            "at": self.var_at.get().strip(),
            "seconds": int(self.var_seconds.get() or 60),
            "time_of_day": self.var_tod.get().strip(),
            "speed": speed,
            "loops": loops,
        }

    def add_task(self) -> None:
        fields = self._collect_task_fields()
        if not fields["file"] or not os.path.exists(fields["file"]):
            messagebox.showwarning("缺少文件", "请选择一个已存在的录制文件。")
            return
        if fields["kind"] == "once" and not fields["at"]:
            messagebox.showwarning("缺少时间", "「指定时刻一次」需要填写时刻。")
            return
        task = self.scheduler.create(**fields)
        if not task.next_run:
            log.warning("任务「%s」的下次执行时间为空，可能时刻已过或格式不对", task.name)
        self.refresh_tasks()

    def update_task(self) -> None:
        selection = self.task_tree.selection()
        if not selection:
            messagebox.showinfo("更新", "请先在列表里选中要更新的任务。")
            return
        task = self.scheduler.get(selection[0])
        if not task:
            return
        for key, value in self._collect_task_fields().items():
            setattr(task, key, value)
        task.refresh_next()
        self.scheduler.save()
        log.info("已更新任务「%s」，下次 %s", task.name, task.next_run)
        self.refresh_tasks()

    def toggle_task(self) -> None:
        selection = self.task_tree.selection()
        if not selection:
            messagebox.showinfo("启用/停用", "请先选中一个任务。")
            return
        task = self.scheduler.get(selection[0])
        if task:
            self.scheduler.set_enabled(task.id, not task.enabled)
            self.refresh_tasks()

    def delete_task(self) -> None:
        selection = self.task_tree.selection()
        if not selection:
            messagebox.showinfo("删除", "请先选中一个任务。")
            return
        task = self.scheduler.get(selection[0])
        if task and messagebox.askyesno("删除", f"确定删除任务「{task.name}」吗？"):
            self.scheduler.remove(task.id)
            self.refresh_tasks()

    def run_task_now(self) -> None:
        selection = self.task_tree.selection()
        if not selection:
            messagebox.showinfo("立即执行", "请先选中一个任务。")
            return
        if self.scheduler.run_now(selection[0]):
            self._append_log("[定时] 已排队立即执行", logging.INFO)

    def _run_scheduled(self, task: Task) -> None:
        """在调度器工作线程里执行一次任务。"""
        if not task.file or not os.path.exists(task.file):
            log.error("任务「%s」的文件不存在：%s", task.name, task.file)
            return
        self._post("task_fired", task)
        self._play_file(
            task.file,
            speed=float(task.speed or 1.0),
            loops=int(task.loops or 1),
            start_delay=1.0,
        )
        # 等待本次回放结束，保证调度器串行执行
        deadline = time.time() + 3600
        while time.time() < deadline:
            player = self.player
            if player is None or not player.is_playing:
                break
            time.sleep(0.2)

    # ==================================================================
    # 状态
    # ==================================================================
    def _update_state_badge(self, recording: bool = False, playing: bool = False) -> None:
        if playing:
            text, color = "回放中", "#1565c0"
        elif recording or self.recorder.recording:
            text, color = f"录制中 · {self.recorder.stop_key.upper()} 停止", "#c62828"
        else:
            text, color = "空闲", "#2e7d32"
        self.lbl_badge.configure(text=text, foreground=color)
        self.btn_record.configure(state="disabled" if self.recorder.recording or playing else "normal")
        self.btn_stop.configure(state="normal" if self.recorder.recording else "disabled")

    # ==================================================================
    # 设置
    # ==================================================================
    def open_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(padx=14, pady=12)

        profile = get_profile(self.config.get("mouse_profile", "standard"))
        var_profile = tk.StringVar(value=profile.label)
        var_overlay = tk.BooleanVar(value=bool(self.config.get("overlay.enabled", True)))
        corner = str(self.config.get("overlay.corner", "top_right"))
        var_corner = tk.StringVar(value=OVERLAY_CORNERS.get(corner, OVERLAY_CORNERS["top_right"]))
        var_color = tk.StringVar(value=str(self.config.get("overlay.text_color", "#d9ffd9")))
        var_alpha = tk.DoubleVar(value=float(self.config.get("overlay.alpha", 0.92)))
        blocked = str(self.config.get("playback.on_input_blocked", "stop"))
        var_blocked = tk.StringVar(value=BLOCKED_POLICIES.get(blocked, BLOCKED_POLICIES["stop"]))

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="鼠标记录").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(
            frame, textvariable=var_profile, state="readonly", width=10,
            values=[p.label for p in PROFILES.values()],
        ).grid(row=0, column=1, sticky="w", padx=(8, 20))

        ttk.Label(frame, text="输入被拦截时").grid(row=0, column=2, sticky="w", pady=4)
        ttk.Combobox(
            frame, textvariable=var_blocked, state="readonly", width=26,
            values=list(BLOCKED_POLICIES.values()),
        ).grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Separator(frame, orient="horizontal").grid(
            row=1, column=0, columnspan=4, sticky="ew", pady=10
        )

        ttk.Checkbutton(frame, text="录制时显示置顶悬浮窗", variable=var_overlay).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=4
        )

        ttk.Label(frame, text="停靠位置").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(
            frame, textvariable=var_corner, state="readonly", width=10,
            values=list(OVERLAY_CORNERS.values()),
        ).grid(row=3, column=1, sticky="w", padx=(8, 20))

        ttk.Label(frame, text="文字颜色").grid(row=3, column=2, sticky="w", pady=4)
        color_row = ttk.Frame(frame)
        color_row.grid(row=3, column=3, sticky="w", padx=(8, 0))

        def pick_color() -> None:
            _rgb, value = colorchooser.askcolor(color=var_color.get(), parent=win)
            if value:
                var_color.set(value)

        ttk.Entry(color_row, textvariable=var_color, width=12).pack(side="left")
        ttk.Button(color_row, text="选择…", command=pick_color).pack(side="left", padx=(6, 0))

        ttk.Label(frame, text="透明度").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Scale(
            frame, from_=0.4, to=1.0, variable=var_alpha, orient="horizontal", length=130,
        ).grid(row=4, column=1, columnspan=2, sticky="w", padx=(8, 0))
        ttk.Label(frame, text="40% – 100%", style="Hint.TLabel").grid(
            row=4, column=3, sticky="w", padx=(8, 0)
        )

        hint = ttk.Label(
            frame,
            text="提示：以管理员身份运行 AutoCat，才能向任务管理器等管理员窗口发送按键和点击。",
            style="Hint.TLabel", wraplength=520, justify="left",
        )
        hint.grid(row=5, column=0, columnspan=4, sticky="w", pady=(12, 6))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=4, sticky="e", pady=(4, 0))

        def apply() -> None:
            selected = get_profile(var_profile.get())
            self.config.set("mouse_profile", selected.key)
            self.config.set("overlay.enabled", bool(var_overlay.get()))
            self.config.set(
                "overlay.corner",
                next((k for k, v in OVERLAY_CORNERS.items() if v == var_corner.get()), "top_right"),
            )
            self.config.set("overlay.text_color", var_color.get())
            self.config.set("overlay.alpha", round(float(var_alpha.get()), 2))
            self.config.set(
                "playback.on_input_blocked",
                next((k for k, v in BLOCKED_POLICIES.items() if v == var_blocked.get()), "stop"),
            )
            self.var_profile.set(selected.label)
            self._on_profile_change()
            try:
                self.overlay.apply_config()
            except Exception:
                log.exception("应用悬浮窗设置失败")
            if self._recording_ui_active:
                if var_overlay.get():
                    self._show_overlay()
                else:
                    self.overlay.hide()
            win.destroy()
            log.info("设置已保存")

        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="保存", command=apply, style="Accent.TButton").pack(
            side="right", padx=(0, 8)
        )

        win.update_idletasks()
        try:
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - win.winfo_width()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - win.winfo_height()) // 3)
            win.geometry(f"+{x}+{y}")
            win.grab_set()
        except Exception:
            pass

    def restart_as_admin(self) -> None:
        if wininput.current_process_elevated():
            messagebox.showinfo("权限", "AutoCat 已经是以管理员身份运行的。")
            return
        if not messagebox.askyesno(
            "以管理员身份重启",
            "重启后 AutoCat 才能向任务管理器等管理员窗口发送输入。\n\n"
            "当前未保存的录制内容会丢失，确定继续吗？",
        ):
            return
        if wininput.relaunch_as_admin():
            log.info("已请求以管理员身份重启")
            self.shutdown()
            try:
                self.root.destroy()
            except Exception:
                pass
        else:
            messagebox.showerror("重启失败", "UAC 被取消或系统拒绝，未能以管理员身份重启。")

    # ==================================================================
    # 帮助 / 退出
    # ==================================================================
    def show_help(self) -> None:
        messagebox.showinfo(
            "快捷键说明",
            "F9      开始录制\n"
            "F10     停止录制并保存\n"
            "F12     中止正在进行的回放\n\n"
            "录制开始后主窗口会最小化，右上角出现半透明状态条：\n"
            "显示鼠标坐标和最近按键，可点「停止」「取消」，也可拖动 / 右键改颜色。\n\n"
            "如果回放时目标窗口以管理员权限运行（例如任务管理器），\n"
            "Windows 会拦截普通权限进程的按键和点击 —— 请以管理员身份运行 AutoCat。",
        )

    def show_about(self) -> None:
        messagebox.showinfo(
            "关于 AutoCat",
            f"AutoCat v{__version__}\n\n"
            "Windows 键鼠自动化工具：录制 / 回放 / 定时执行。\n"
            "录制文件格式：JSON、XML（可互相导入）。\n\n"
            f"录制目录：{self.rec_dir}\n"
            f"任务文件：{self.schedule_path}",
        )

    def shutdown(self) -> None:
        """停掉所有后台线程和监听器（关窗和自动化测试都走这里）。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            if self.overlay is not None:
                self.overlay.destroy()
        except Exception:
            pass
        try:
            if self.player is not None and self.player.is_playing:
                self.player.abort()
        except Exception:
            pass
        for step, action in (
            ("调度器", self.scheduler.stop),
            ("录制器", self.recorder.shutdown),
        ):
            try:
                action()
            except Exception:
                log.exception("关闭%s时出错", step)

        if self._log_handler is not None:
            logging.getLogger("autocat").removeHandler(self._log_handler)
            self._log_handler = None

        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

    def on_close(self) -> None:
        if not messagebox.askokcancel("退出", "确定退出 AutoCat 吗？"):
            return
        log.info("正在退出…")
        self.shutdown()
        self.root.destroy()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run(base_dir: str | None = None) -> int:
    """启动图形界面。"""
    base_dir = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    enable_dpi_awareness()          # 必须在 Tk() 之前
    set_app_user_model_id()
    root = tk.Tk()
    AutoCatGUI(root, base_dir)
    root.mainloop()
    return 0
