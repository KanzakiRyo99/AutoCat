"""录制状态悬浮窗。

开始录制时主界面会最小化，屏幕上留下这个小窗口，用来看：

- 当前状态与已录制时长
- 鼠标实时坐标
- 最近按下的键
- 已记录的事件数
- 「停止并保存」「取消录制」两个按钮

窗口无边框、置顶、半透明，可以拖动；文字颜色、透明度、停靠角落都可以在
右键菜单里改，设置会写回 config.json。悬浮窗覆盖区域内的鼠标事件会被录制器
忽略，所以点按钮不会把自己录进去。
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import colorchooser
from typing import Callable, Optional

from .config import OVERLAY_CORNERS, Config
from .recorder import Recorder

log = logging.getLogger("autocat.overlay")

#: 好看一点的按键显示名
_KEY_DISPLAY = {
    "ctrl_l": "Ctrl", "ctrl_r": "Ctrl", "shift_l": "Shift", "shift_r": "Shift",
    "alt_l": "Alt", "alt_r": "Alt", "cmd_l": "Win", "cmd_r": "Win",
    "enter": "Enter", "space": "Space", "tab": "Tab", "esc": "Esc",
    "backspace": "Bksp", "delete": "Del", "up": "↑", "down": "↓",
    "left": "←", "right": "→", "page_up": "PgUp", "page_down": "PgDn",
}


def pretty_key(name: str) -> str:
    text = str(name)
    if text in _KEY_DISPLAY:
        return _KEY_DISPLAY[text]
    if text.startswith("vk:"):
        return text
    if len(text) == 1:
        return text.upper()
    return text


class _PillButton(tk.Canvas):
    """Canvas 画的圆角按钮，比 tk.Button 更贴合圆润的悬浮窗。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        fill: str,
        fg: str,
        hover: str,
        bg: str,
        font: tuple,
        radius: int = 12,
        width: int = 80,
        height: int = 38,
    ) -> None:
        super().__init__(
            master, width=width, height=height, bg=bg, bd=0,
            highlightthickness=0, cursor="hand2", takefocus=0,
        )
        self._text = text
        self._command = command
        self._fill = fill
        self._fg = fg
        self._hover = hover
        self._font = font
        self._radius = radius
        self._hovering = False
        self._bg = bg
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def set_palette(self, fill: str, fg: str, hover: str, bg: str) -> None:
        self._fill = fill
        self._fg = fg
        self._hover = hover
        self._bg = bg
        self.configure(bg=bg)
        self._redraw()

    def _on_enter(self, _event) -> None:
        self._hovering = True
        self._redraw()

    def _on_leave(self, _event) -> None:
        self._hovering = False
        self._redraw()

    def _on_click(self, _event) -> None:
        self._command()

    def _redraw(self) -> None:
        width = max(2, self.winfo_width())
        height = max(2, self.winfo_height())
        radius = min(self._radius, height // 2, width // 2)
        fill = self._hover if self._hovering else self._fill
        self.delete("all")
        # 用四段圆角和两块矩形拼出圆角矩形
        self.create_rectangle(radius, 0, width - radius, height, fill=fill, outline=fill)
        self.create_rectangle(0, radius, width, height - radius, fill=fill, outline=fill)
        diameter = radius * 2
        for x, y, start in (
            (0, 0, 90),
            (width - diameter, 0, 0),
            (0, height - diameter, 180),
            (width - diameter, height - diameter, 270),
        ):
            self.create_arc(
                x, y, x + diameter, y + diameter,
                start=start, extent=90, fill=fill, outline=fill, style="pieslice",
            )
        self.create_text(
            width // 2, height // 2, text=self._text, fill=self._fg,
            font=self._font, anchor="center",
        )


class RecorderOverlay:
    """置顶的半透明录制状态卡。"""

    BG = "#0f1714"
    BORDER = "#2b4036"
    TEXT = "#eef6f2"
    MUTED = "#8ba398"
    ACCENT = "#5ce0a6"
    DANGER = "#ff5d5d"
    DANGER_DARK = "#c94141"

    def __init__(
        self,
        master: tk.Misc,
        recorder: Recorder,
        config: Config,
        on_stop: Callable[[], None],
        on_cancel: Callable[[], None],
        on_rect_change: Optional[Callable[[Optional[tuple]], None]] = None,
    ) -> None:
        self.master = master
        self.recorder = recorder
        self.config = config
        self.on_stop = on_stop
        self.on_cancel = on_cancel
        self.on_rect_change = on_rect_change or (lambda rect: None)

        self._visible = False
        self._tick_id = None
        self._pulse_id = None
        self._pulse_on = True
        self._drag_offset: Optional[tuple] = None
        self._text_color = str(config.get("overlay.text_color", "#d9ffd9"))

        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self._scale = max(1.0, float(self.win.winfo_fpixels("1i")) / 96.0)
        except Exception:
            self._scale = 1.0
        self._build()
        self._apply_style()
        self.win.update_idletasks()
        self._detach_owner()

    def _detach_owner(self) -> None:
        """把悬浮窗从主窗口的「owned window」关系里摘出来。

        Tk 的 Toplevel 默认属于主窗口。Windows 在 owner 最小化时会连 owned
        窗口一起隐藏 —— 而我们的用法恰好是「主窗口最小化、只留悬浮窗」。
        这里用 Win32 把 owner 置空，悬浮窗就能独立显示，也不会跟着最小化。
        """
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            user32.GetAncestor.restype = ctypes.c_void_p
            client = int(self.win.winfo_id())
            root_hwnd = user32.GetAncestor(client, 2)  # GA_ROOT
            if not root_hwnd:
                root_hwnd = client
            GWLP_HWNDPARENT = -8
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                setter = user32.SetWindowLongPtrW
                setter.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
                setter.restype = ctypes.c_void_p
                setter(root_hwnd, GWLP_HWNDPARENT, None)
            else:  # pragma: no cover - 32 位解释器
                user32.SetWindowLongW(root_hwnd, GWLP_HWNDPARENT, 0)
            log.debug("悬浮窗已解除 owner 关系")
        except Exception:
            log.exception("解除悬浮窗 owner 关系失败（不影响其它功能）")

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _px(self, value: float) -> int:
        return max(1, int(round(value * self._scale)))

    def _build(self) -> None:
        self.frame = tk.Frame(self.win, bd=0, highlightthickness=1)
        self.frame.pack(fill="both", expand=True)

        # 顶部细红条：一眼能看出正在录制
        self.strip = tk.Frame(self.frame, height=3)
        self.strip.pack(fill="x", side="top")

        header = tk.Frame(self.frame)
        header.pack(fill="x", padx=16, pady=(10, 8))

        self.dot = tk.Label(header, text="●", font=("Segoe UI", 14), width=1)
        self.dot.pack(side="left")

        self.title = tk.Label(
            header, text="录制中", font=("Microsoft YaHei UI", 10, "bold"), anchor="w"
        )
        self.title.pack(side="left", padx=(4, 8))

        self.timer = tk.Label(
            header, text="00:00.0", font=("Consolas", 18, "bold"), anchor="e"
        )
        self.timer.pack(side="right")

        self.separator = tk.Frame(self.frame, height=1)
        self.separator.pack(fill="x", padx=16)

        body = tk.Frame(self.frame)
        body.pack(fill="x", padx=16, pady=(9, 5))
        body.columnconfigure(1, weight=1)

        self.lbl_pos_key = tk.Label(
            body, text="鼠标", font=("Microsoft YaHei UI", 9), anchor="w"
        )
        self.lbl_pos_key.grid(row=0, column=0, sticky="w", pady=3)
        self.lbl_pos = tk.Label(
            body, text="—, —", font=("Consolas", 11), anchor="w", justify="left"
        )
        self.lbl_pos.grid(row=0, column=1, sticky="w", padx=(12, 0), pady=3)

        self.lbl_keys_key = tk.Label(
            body, text="按键", font=("Microsoft YaHei UI", 9), anchor="w"
        )
        self.lbl_keys_key.grid(row=1, column=0, sticky="w", pady=3)
        self.lbl_keys = tk.Label(
            body, text="—", font=("Consolas", 10), anchor="w", justify="left"
        )
        self.lbl_keys.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=3)

        self.lbl_count = tk.Label(
            body, text="已记录 0 条 · F10 停止 · 右键可改颜色",
            font=("Microsoft YaHei UI", 8), anchor="w",
        )
        self.lbl_count.grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))

        footer = tk.Frame(self.frame)
        footer.pack(fill="x", padx=16, pady=(10, 14))

        self.btn_stop = _PillButton(
            footer, text="■  停止并保存", command=self._stop,
            fill=self.DANGER, fg="#ffffff", hover=self.DANGER_DARK, bg=self.BG,
            font=("Microsoft YaHei UI", 9, "bold"),
            radius=self._px(14), height=self._px(40),
        )
        self.btn_stop.pack(side="left", fill="x", expand=True)

        self.btn_cancel = _PillButton(
            footer, text="✕", command=self._cancel,
            fill="#26352e", fg=self.MUTED, hover="#3a4d43", bg=self.BG,
            font=("Segoe UI", 10), radius=self._px(14),
            width=self._px(48), height=self._px(40),
        )
        self.btn_cancel.pack(side="left", padx=(10, 0))

        self._drag_bindings = [
            self.frame, self.strip, header, self.dot, self.title, self.timer,
            self.separator, body, self.lbl_pos_key, self.lbl_pos,
            self.lbl_keys_key, self.lbl_keys, self.lbl_count,
        ]
        for widget in self._drag_bindings:
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_motion)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Button-3>", self._popup_menu)

        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu.add_command(label="文字颜色…", command=self._choose_color)
        self.menu.add_command(label="恢复默认颜色", command=self._reset_color)
        self.menu.add_separator()
        self.menu.add_command(label="切换到左上角", command=lambda: self._set_corner("top_left"))
        self.menu.add_command(label="切换到右上角", command=lambda: self._set_corner("top_right"))
        alpha_menu = tk.Menu(self.menu, tearoff=0)
        for percent in (70, 80, 86, 92, 100):
            alpha_menu.add_command(
                label=f"{percent}%", command=lambda p=percent: self._set_alpha(p / 100)
            )
        self.menu.add_cascade(label="透明度", menu=alpha_menu)

    def _apply_style(self) -> None:
        color = str(self.config.get("overlay.text_color", self._text_color))
        self._text_color = color
        alpha = float(self.config.get("overlay.alpha", 0.92) or 0.92)
        bg = self.BG

        self.win.configure(bg=bg)
        self.win.attributes("-alpha", max(0.3, min(1.0, alpha)))
        self.frame.configure(bg=bg, highlightbackground=self.BORDER)
        self.strip.configure(bg=self.DANGER)
        for widget in (self.frame, self.strip, self.separator):
            widget.configure(bg=bg if widget is not self.strip else self.DANGER)
        for widget in self.frame.winfo_children():
            if isinstance(widget, tk.Frame) and widget is not self.strip:
                widget.configure(bg=bg)

        self.dot.configure(bg=bg, fg=self.DANGER if self._pulse_on else "#7d2d2d")
        self.title.configure(bg=bg, fg=self.TEXT)
        self.timer.configure(bg=bg, fg=self.ACCENT)
        self.separator.configure(bg=self.BORDER)
        for widget in (self.lbl_pos_key, self.lbl_keys_key):
            widget.configure(bg=bg, fg=self.MUTED)
        self.lbl_pos.configure(bg=bg, fg=color)
        self.lbl_keys.configure(bg=bg, fg=color)
        self.lbl_count.configure(bg=bg, fg=self.MUTED)
        self.btn_stop.set_palette(
            self.DANGER, "#ffffff", self.DANGER_DARK, bg,
        )
        self.btn_cancel.set_palette("#26352e", self.MUTED, "#3a4d43", bg)

    def apply_config(self) -> None:
        """设置对话框保存后调用，立即生效。"""
        self._apply_style()
        self._place()
        self._update_rect()

    # ------------------------------------------------------------------
    # 显示 / 隐藏
    # ------------------------------------------------------------------
    @property
    def visible(self) -> bool:
        return self._visible

    def show(self) -> None:
        if self._visible:
            return
        self._visible = True
        self._pulse_on = True
        self.win.update_idletasks()
        self._place()
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.update_idletasks()
        self._round_corners()
        self._update_rect()
        self._tick()
        self._pulse()

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        for timer_id in (self._tick_id, self._pulse_id):
            if timer_id is not None:
                try:
                    self.win.after_cancel(timer_id)
                except Exception:
                    pass
        self._tick_id = None
        self._pulse_id = None
        self.win.withdraw()
        self._update_rect()
        self.on_rect_change(None)

    def destroy(self) -> None:
        self.hide()
        try:
            self.win.destroy()
        except Exception:
            pass

    def _place(self) -> None:
        self.win.update_idletasks()
        width = max(380, self.win.winfo_reqwidth())
        height = max(190, self.win.winfo_reqheight())
        screen_w = self.win.winfo_screenwidth()
        corner = str(self.config.get("overlay.corner", "top_right"))
        if corner not in OVERLAY_CORNERS:
            corner = "top_right"
        margin = 16
        x = margin if corner == "top_left" else max(0, screen_w - width - margin)
        self.win.geometry(f"{width}x{height}+{x}+{margin}")
        self.win.update_idletasks()
        self._round_corners()

    def _round_corners(self) -> None:
        """用窗口区域把矩形裁成圆角卡片。"""
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            user32.GetAncestor.restype = ctypes.c_void_p
            client = int(self.win.winfo_id())
            hwnd = user32.GetAncestor(client, 2) or client
            width = max(1, int(self.win.winfo_width()))
            height = max(1, int(self.win.winfo_height()))
            radius = self._px(22)

            user32.CreateRoundRectRgn.argtypes = (
                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_int,
            )
            user32.CreateRoundRectRgn.restype = ctypes.c_void_p
            region = user32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
            if not region:
                return
            user32.SetWindowRgn.argtypes = (
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool,
            )
            user32.SetWindowRgn.restype = ctypes.c_int
            if not user32.SetWindowRgn(hwnd, region, True):
                ctypes.windll.gdi32.DeleteObject(region)
        except Exception:
            log.debug("设置悬浮窗圆角失败", exc_info=True)

    def _update_rect(self) -> None:
        if not self._visible:
            return
        try:
            x = self.win.winfo_rootx()
            y = self.win.winfo_rooty()
            w = self.win.winfo_width()
            h = self.win.winfo_height()
        except Exception:
            return
        rect = (x - 2, y - 2, x + w + 2, y + h + 2)
        self.on_rect_change(rect)

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------
    def _pulse(self) -> None:
        if not self._visible:
            return
        self._pulse_on = not self._pulse_on
        self.dot.configure(fg=self.DANGER if self._pulse_on else "#7d2d2d")
        self._pulse_id = self.win.after(550, self._pulse)

    def _tick(self) -> None:
        if not self._visible:
            return
        snap = self.recorder.snapshot()
        elapsed = float(snap["elapsed"])
        self.timer.configure(text=f"{int(elapsed // 60):02d}:{elapsed % 60:04.1f}")

        pos = snap.get("pos")
        self.lbl_pos.configure(text=f"{pos[0]}, {pos[1]}" if pos else "—, —")

        keys = snap.get("keys") or []
        show = int(self.config.get("overlay.show_keys", 3) or 3)
        recent = " → ".join(pretty_key(k) for k in keys[-show:])
        self.lbl_keys.configure(text=recent or "—")
        self.lbl_count.configure(
            text=f"已记录 {snap.get('events', 0)} 条 · F10 停止 · 右键可改颜色"
        )

        if not snap.get("recording"):
            # 录制已经由热键结束，界面状态由主线程统一收尾
            self.hide()
            return
        self._update_rect()
        self._tick_id = self.win.after(100, self._tick)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _stop(self) -> None:
        try:
            self.on_stop()
        finally:
            self.hide()

    def _cancel(self) -> None:
        try:
            self.on_cancel()
        finally:
            self.hide()

    def _drag_start(self, event) -> None:
        self._drag_offset = (event.x_root - self.win.winfo_x(),
                             event.y_root - self.win.winfo_y())

    def _drag_motion(self, event) -> None:
        if not self._drag_offset:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.win.geometry(f"+{x}+{y}")
        self._update_rect()

    def _drag_end(self, _event) -> None:
        self._drag_offset = None
        self._update_rect()

    def _popup_menu(self, event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _choose_color(self) -> None:
        _rgb, color = colorchooser.askcolor(
            color=self._text_color, title="悬浮窗文字颜色", parent=self.win
        )
        if color:
            self.config.set("overlay.text_color", color)
            self._apply_style()

    def _reset_color(self) -> None:
        self.config.set("overlay.text_color", "#d9ffd9")
        self._apply_style()

    def _set_corner(self, corner: str) -> None:
        self.config.set("overlay.corner", corner)
        self._place()
        self._update_rect()

    def _set_alpha(self, alpha: float) -> None:
        self.config.set("overlay.alpha", round(alpha, 2))
        self._apply_style()
