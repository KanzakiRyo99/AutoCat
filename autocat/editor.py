"""录制文件的图形化查看 / 编辑。

把 JSON / XML 里的 ``events`` 摊开成一张表：

- 连续的 ``mouse_move`` 会合并成一个可折叠的「鼠标移动 · N 个点」分组
  （默认折叠，双击展开），避免几百行坐标刷屏；
- 键盘、点击、滚轮各占一行，双击即可修改字段；
- 可以在任意事件的前面或后面插入一条新事件；
- 保存时仍然写回原格式（``.json`` / ``.xml``），数据模型不变。

编辑器只操作 ``events`` 列表，不改变文件结构，所以旧文件随时可以继续用
命令行回放。
"""

from __future__ import annotations

import copy
import logging
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

from . import motion, storage
from .player import key_from_str

log = logging.getLogger("autocat.editor")

TYPE_LABELS = {
    "key_press": "键盘按下",
    "key_release": "键盘松开",
    "mouse_move": "鼠标移动",
    "mouse_click": "鼠标点击",
    "mouse_scroll": "鼠标滚轮",
}
LABEL_TYPES = {label: key for key, label in TYPE_LABELS.items()}

BUTTONS = {
    "left": "左键",
    "right": "右键",
    "middle": "中键",
    "x1": "侧键 1",
    "x2": "侧键 2",
}
BUTTON_KEYS = list(BUTTONS)


def _fmt_t(value: float) -> str:
    return f"{float(value):.3f}"


def _pretty_key(name: str) -> str:
    text = str(name)
    if len(text) == 1:
        return text
    return text


# ---------------------------------------------------------------------------
# 单条事件编辑对话框
# ---------------------------------------------------------------------------
class EventDialog(tk.Toplevel):
    """新增 / 修改一条事件的模态对话框。"""

    def __init__(
        self,
        parent: tk.Misc,
        event: Optional[dict] = None,
        default_type: str = "key_press",
        default_t: float = 0.0,
        title: str = "编辑事件",
    ) -> None:
        super().__init__(parent)
        self.result: Optional[dict] = None
        self._event = dict(event) if event else None
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.configure(padx=12, pady=10)

        self.var_type = tk.StringVar(
            value=TYPE_LABELS.get((event or {}).get("type", default_type), "键盘按下")
        )
        self.var_t = tk.StringVar(value=str((event or {}).get("t", default_t)))
        self.var_key = tk.StringVar(value=str((event or {}).get("key", "")))
        self.var_x = tk.StringVar(value=str((event or {}).get("x", 0)))
        self.var_y = tk.StringVar(value=str((event or {}).get("y", 0)))
        self.var_button = tk.StringVar(
            value=BUTTONS.get(str((event or {}).get("button", "left")), "左键")
        )
        self.var_pressed = tk.BooleanVar(value=bool((event or {}).get("pressed", True)))
        self.var_dx = tk.StringVar(value=str((event or {}).get("dx", 0)))
        self.var_dy = tk.StringVar(value=str((event or {}).get("dy", -1)))

        self._build()
        self._on_type_change()

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.update_idletasks()
        self._center(parent)
        try:
            self.grab_set()
        except Exception:
            pass
        self.wait_window(self)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        row = 0
        ttk.Label(self, text="类型").grid(row=row, column=0, sticky="w", pady=3)
        self.cmb_type = ttk.Combobox(
            self, textvariable=self.var_type, state="readonly", width=14,
            values=[TYPE_LABELS[t] for t in TYPE_LABELS],
        )
        self.cmb_type.grid(row=row, column=1, sticky="w", padx=(6, 20), pady=3)
        self.cmb_type.bind("<<ComboboxSelected>>", lambda _e: self._on_type_change())

        ttk.Label(self, text="时间 (秒)").grid(row=row, column=2, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.var_t, width=12).grid(
            row=row, column=3, sticky="w", padx=(6, 0), pady=3
        )

        row += 1
        self.lbl_key = ttk.Label(self, text="按键")
        self.lbl_key.grid(row=row, column=0, sticky="w", pady=3)
        self.ent_key = ttk.Entry(self, textvariable=self.var_key, width=18)
        self.ent_key.grid(row=row, column=1, sticky="w", padx=(6, 20), pady=3)
        self.lbl_key_hint = ttk.Label(
            self, text="如 a / enter / ctrl_l / vk:81", style="Hint.TLabel"
        )
        self.lbl_key_hint.grid(row=row, column=2, columnspan=2, sticky="w")

        row += 1
        self.lbl_xy = ttk.Label(self, text="坐标 x, y")
        self.lbl_xy.grid(row=row, column=0, sticky="w", pady=3)
        xy_frame = ttk.Frame(self)
        self.ent_x = ttk.Entry(xy_frame, textvariable=self.var_x, width=8)
        self.ent_y = ttk.Entry(xy_frame, textvariable=self.var_y, width=8)
        self.ent_x.pack(side="left")
        ttk.Label(xy_frame, text=",").pack(side="left", padx=4)
        self.ent_y.pack(side="left")
        self._xy_frame = xy_frame
        xy_frame.grid(row=row, column=1, sticky="w", padx=(6, 20), pady=3)

        self.lbl_button = ttk.Label(self, text="按键")
        self.lbl_button.grid(row=row, column=2, sticky="w", pady=3)
        btn_frame = ttk.Frame(self)
        self.cmb_button = ttk.Combobox(
            btn_frame, textvariable=self.var_button, state="readonly", width=8,
            values=list(BUTTONS.values()),
        )
        self.cmb_button.pack(side="left")
        self.chk_pressed = ttk.Checkbutton(
            btn_frame, text="按下", variable=self.var_pressed
        )
        self.chk_pressed.pack(side="left", padx=(8, 0))
        self._btn_frame = btn_frame
        btn_frame.grid(row=row, column=3, sticky="w", padx=(6, 0), pady=3)

        row += 1
        self.lbl_scroll = ttk.Label(self, text="滚轮 dx, dy")
        self.lbl_scroll.grid(row=row, column=0, sticky="w", pady=3)
        scroll_frame = ttk.Frame(self)
        self.ent_dx = ttk.Entry(scroll_frame, textvariable=self.var_dx, width=8)
        self.ent_dy = ttk.Entry(scroll_frame, textvariable=self.var_dy, width=8)
        self.ent_dx.pack(side="left")
        ttk.Label(scroll_frame, text=",").pack(side="left", padx=4)
        self.ent_dy.pack(side="left")
        self._scroll_frame = scroll_frame
        scroll_frame.grid(row=row, column=1, sticky="w", padx=(6, 20), pady=3)

        self._xy_label = self.lbl_xy

        row += 1
        buttons = ttk.Frame(self)
        buttons.grid(row=row, column=0, columnspan=4, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="确定", command=self._ok, style="Accent.TButton").pack(
            side="right", padx=(0, 8)
        )

    def _center(self, parent: tk.Misc) -> None:
        try:
            x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _current_type(self) -> str:
        return LABEL_TYPES.get(self.var_type.get(), "key_press")

    def _on_type_change(self) -> None:
        etype = self._current_type()
        widgets = (
            self.lbl_key, self.ent_key, self.lbl_key_hint,
            self.lbl_xy, self._xy_frame,
            self.lbl_button, self._btn_frame,
            self.lbl_scroll, self._scroll_frame,
        )
        for widget in widgets:
            widget.grid_remove()

        if etype in ("key_press", "key_release"):
            self.lbl_key.grid()
            self.ent_key.grid()
            self.lbl_key_hint.grid()
            return

        self.lbl_xy.grid()
        self._xy_frame.grid()
        if etype == "mouse_click":
            self.lbl_button.grid()
            self._btn_frame.grid()
        elif etype == "mouse_scroll":
            self.lbl_scroll.grid()
            self._scroll_frame.grid()

    # ------------------------------------------------------------------
    def _ok(self) -> None:
        try:
            event = self._collect()
        except ValueError as exc:
            messagebox.showwarning("数值不对", str(exc), parent=self)
            return
        self.result = event
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _collect(self) -> dict:
        etype = self._current_type()
        try:
            t = float(self.var_t.get())
        except ValueError:
            raise ValueError("时间必须是数字，例如 1.25")
        if t < 0:
            raise ValueError("时间不能是负数")

        event: dict = {"t": round(t, 4), "type": etype}

        if etype in ("key_press", "key_release"):
            key = self.var_key.get().strip()
            if not key:
                raise ValueError("按键不能为空")
            if key_from_str(key) is None:
                raise ValueError(f"无法识别的按键：{key}")
            event["key"] = key
            return event

        try:
            x = int(float(self.var_x.get()))
            y = int(float(self.var_y.get()))
        except ValueError:
            raise ValueError("坐标必须是整数")
        event["x"] = x
        event["y"] = y

        if etype == "mouse_click":
            label = self.var_button.get()
            event["button"] = next(
                (k for k, v in BUTTONS.items() if v == label), "left"
            )
            event["pressed"] = bool(self.var_pressed.get())
        elif etype == "mouse_scroll":
            try:
                dx = int(float(self.var_dx.get() or 0))
                dy = int(float(self.var_dy.get() or 0))
            except ValueError:
                raise ValueError("滚轮增量必须是整数")
            event["dx"] = dx
            event["dy"] = dy
        return event


# ---------------------------------------------------------------------------
# 编辑器
# ---------------------------------------------------------------------------
class RecordingEditor(ttk.Frame):
    """录制内容查看 / 编辑面板。"""

    def __init__(self, master: tk.Misc, on_saved=None) -> None:
        super().__init__(master, padding=8)
        self.on_saved = on_saved

        self.path: Optional[str] = None
        self.payload: Optional[dict] = None
        self.events: List[dict] = []
        self.dirty = False
        self._undo: List[List[dict]] = []
        self._open_starts: set = set()
        self._groups: Dict[str, Tuple[int, int]] = {}
        self._row_event: Dict[str, int] = {}
        self._profile_key = "standard"

        self._build()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x")

        ttk.Label(top, text="录制内容", style="Title.TLabel").pack(side="left")
        self.lbl_file = ttk.Label(top, text="未打开文件", style="Hint.TLabel")
        self.lbl_file.pack(side="left", padx=(10, 0))

        self.lbl_dirty = ttk.Label(self, text="", foreground="#b3261e")
        self.lbl_dirty.pack(side="right")

        # 第一行：文件操作
        file_bar = ttk.Frame(self)
        file_bar.pack(fill="x", pady=(8, 4))
        ttk.Button(file_bar, text="保存", command=self.save, style="Accent.TButton").pack(
            side="left"
        )
        ttk.Button(file_bar, text="重新载入", command=self.reload).pack(side="left", padx=6)
        ttk.Button(file_bar, text="另存为…", command=self.save_as).pack(side="left")
        ttk.Button(file_bar, text="查看源码", command=self.show_source).pack(side="left", padx=6)
        ttk.Button(file_bar, text="撤销", command=self.undo).pack(side="left")

        # 第二行：查看与精简
        view_bar = ttk.Frame(self)
        view_bar.pack(fill="x", pady=(0, 6))
        ttk.Button(view_bar, text="全部展开", command=lambda: self.expand_all(True)).pack(
            side="left"
        )
        ttk.Button(view_bar, text="全部折叠", command=lambda: self.expand_all(False)).pack(
            side="left", padx=6
        )
        ttk.Separator(view_bar, orient="vertical").pack(side="left", fill="y", padx=12)

        ttk.Label(view_bar, text="精简档").pack(side="left", padx=(0, 5))
        self.cmb_profile = ttk.Combobox(
            view_bar, state="readonly", width=6,
            values=[p.label for p in motion.PROFILES.values()],
        )
        self.cmb_profile.set(motion.get_profile(self._profile_key).label)
        self.cmb_profile.pack(side="left")
        ttk.Button(view_bar, text="精简鼠标移动", command=self.simplify_moves).pack(
            side="left", padx=6
        )

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)

        columns = ("time", "type", "detail", "note")
        self.tree = ttk.Treeview(wrap, columns=columns, show="tree headings", height=8)
        self.tree.heading("#0", text="事件")
        self.tree.heading("time", text="时间 (秒)")
        self.tree.heading("type", text="类型")
        self.tree.heading("detail", text="内容")
        self.tree.heading("note", text="说明")
        self.tree.column("#0", width=self._px(250), anchor="w")
        self.tree.column("time", width=self._px(90), anchor="center")
        self.tree.column("type", width=self._px(100), anchor="center")
        self.tree.column("detail", width=self._px(420), anchor="w")
        self.tree.column("note", width=self._px(110), anchor="center")
        self.tree.tag_configure("group", background="#eaf4ef", foreground="#1f4f3f")
        self.tree.tag_configure("odd", background="#f7faf8")
        self.tree.tag_configure("even", background="#ffffff")

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Delete>", lambda _e: self.delete_selected())
        self.tree.bind("<F2>", lambda _e: self.edit_selected())
        self.tree.bind("<Control-s>", self._on_ctrl_s)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="▲ 在上方插入", command=lambda: self.insert_event(True)).pack(
            side="left"
        )
        ttk.Button(bottom, text="▼ 在下方插入", command=lambda: self.insert_event(False)).pack(
            side="left", padx=6
        )
        ttk.Button(bottom, text="编辑选中", command=self.edit_selected).pack(side="left")
        ttk.Button(
            bottom, text="删除选中", command=self.delete_selected, style="Danger.TButton"
        ).pack(side="left", padx=6)

        self.lbl_status = ttk.Label(bottom, text="请选择一个录制文件", style="Hint.TLabel")
        self.lbl_status.pack(side="right")

    @staticmethod
    def _px(value: int) -> int:
        # 这里不依赖主窗口的 DPI 缩放，交给 ttk 自己处理行高即可
        return value

    def _on_ctrl_s(self, _event=None) -> None:
        self.save()

    # ------------------------------------------------------------------
    # 载入 / 保存
    # ------------------------------------------------------------------
    def load_path(self, path: str) -> bool:
        try:
            payload = storage.load_recording(path)
        except Exception as exc:
            log.exception("读取录制文件失败: %s", path)
            messagebox.showerror("读取失败", f"{path}\n\n{exc}", parent=self)
            return False
        self.path = path
        self.payload = payload
        self.events = [dict(e) for e in payload.get("events", [])]
        self.dirty = False
        self._undo.clear()
        self._open_starts.clear()
        self._rebuild()
        self._refresh_header()
        return True

    def reload(self) -> None:
        if not self.path:
            return
        if self.dirty and not messagebox.askyesno(
            "放弃修改", "当前文件有未保存的修改，确定重新载入吗？", parent=self
        ):
            return
        self.load_path(self.path)

    def save(self, path: Optional[str] = None) -> bool:
        path = path or self.path
        if not path:
            return self.save_as()
        if not self.events:
            messagebox.showwarning("没有内容", "事件列表是空的，没有可保存的内容。", parent=self)
            return False

        # 按时间排序后再写，保证文件和界面顺序一致
        self.events.sort(key=lambda e: float(e.get("t", 0.0)))
        payload = dict(self.payload or {})
        payload["events"] = self.events
        meta = payload.get("meta")
        if isinstance(meta, dict) and "events" in meta:
            meta["events"] = len(self.events)
        try:
            storage.save_recording(path, payload)
        except Exception as exc:
            log.exception("保存失败: %s", path)
            messagebox.showerror("保存失败", str(exc), parent=self)
            return False

        self.path = path
        self.payload = storage.load_recording(path)
        self.events = [dict(e) for e in self.payload.get("events", [])]
        self.dirty = False
        self._rebuild()
        self._refresh_header()
        log.info("录制内容已保存: %s（%d 条事件）", path, len(self.events))
        if self.on_saved:
            try:
                self.on_saved()
            except Exception:
                log.exception("on_saved 回调失败")
        return True

    def save_as(self) -> bool:
        if not self.path:
            initial = os.path.join(os.getcwd(), "recording.json")
        else:
            initial = self.path
        path = filedialog.asksaveasfilename(
            parent=self,
            title="另存录制文件",
            initialfile=os.path.basename(initial),
            initialdir=os.path.dirname(initial) or os.getcwd(),
            defaultextension=".json",
            filetypes=[("JSON 录制", "*.json"), ("XML 录制", "*.xml"), ("所有文件", "*.*")],
        )
        if not path:
            return False
        return self.save(path)

    # ------------------------------------------------------------------
    # 表格
    # ------------------------------------------------------------------
    def _clear_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())

    def _rebuild(self, select_event: Optional[int] = None, keep_open: Optional[set] = None) -> None:
        open_starts = self._open_starts if keep_open is None else set(keep_open)
        selected = select_event
        self._clear_tree()
        self._groups.clear()
        self._row_event.clear()
        self._open_starts = set()

        index = 0
        total = len(self.events)
        while index < total:
            event = self.events[index]
            if event.get("type") == "mouse_move":
                end = index + 1
                while end < total and self.events[end].get("type") == "mouse_move":
                    end += 1
                count = end - index
                if count >= 2:
                    self._insert_group(index, end, count, open_=index in open_starts)
                    if index in open_starts:
                        self._open_starts.add(index)
                    index = end
                    continue
            self._insert_row(index, event)
            index += 1

        if selected is not None and self.tree.exists(f"e:{selected}"):
            self.tree.selection_set(f"e:{selected}")
            self.tree.see(f"e:{selected}")
        self._refresh_status()

    def _insert_group(self, start: int, end: int, count: int, open_: bool) -> None:
        t0 = float(self.events[start].get("t", 0))
        t1 = float(self.events[end - 1].get("t", 0))
        gid = f"g:{start}"
        self._groups[gid] = (start, end)
        self.tree.insert(
            "", "end", iid=gid, text=f"鼠标移动 · {count} 个点",
            values=(
                f"{t0:.2f} ~ {t1:.2f}", "鼠标移动", f"{count} 个点",
                "已展开" if open_ else "双击展开",
            ),
            open=open_, tags=("group",),
        )
        for index in range(start, end):
            event = self.events[index]
            self._insert_row(index, event, parent=gid)

    def _insert_row(self, index: int, event: dict, parent: str = "") -> None:
        iid = f"e:{index}"
        self._row_event[iid] = index
        values = self._event_values(event)
        prefix = "  " if parent else ""
        tags = ("even",) if parent else ("odd" if index % 2 else "even",)
        self.tree.insert(
            parent, "end", iid=iid, text=f"{prefix}#{index + 1}", values=values,
            tags=tags,
        )

    def _event_values(self, event: dict) -> tuple:
        etype = event.get("type", "")
        label = TYPE_LABELS.get(etype, etype)
        t = _fmt_t(event.get("t", 0))
        if etype in ("key_press", "key_release"):
            key = str(event.get("key", ""))
            detail = key
            note = "字符" if len(key) == 1 else "特殊键"
        elif etype == "mouse_click":
            button = BUTTONS.get(str(event.get("button", "")), str(event.get("button", "")))
            state = "按下" if event.get("pressed", True) else "松开"
            detail = f"({event.get('x', 0)}, {event.get('y', 0)}) · {button} · {state}"
            note = "点击"
        elif etype == "mouse_scroll":
            detail = (
                f"({event.get('x', 0)}, {event.get('y', 0)}) · "
                f"dx={event.get('dx', 0)}, dy={event.get('dy', 0)}"
            )
            note = "滚轮"
        elif etype == "mouse_move":
            detail = f"x={event.get('x', 0)}, y={event.get('y', 0)}"
            note = ""
        else:
            detail = str(event)
            note = "?"
        return (t, label, detail, note)

    def _refresh_header(self) -> None:
        if not self.path:
            self.lbl_file.configure(text="未打开文件")
            return
        name = os.path.basename(self.path)
        fmt = storage.detect_format(self.path).upper()
        created = str((self.payload or {}).get("created_at", "")).replace("T", " ")
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        self.lbl_file.configure(
            text=f"{name} · {fmt} · {size / 1024:.1f} KB · 录制于 {created or '未知'}"
        )
        self._refresh_dirty()

    def _refresh_status(self) -> None:
        moves = sum(1 for e in self.events if e.get("type") == "mouse_move")
        groups = len(self._groups)
        self.lbl_status.configure(
            text=f"{len(self.events)} 条事件 · 鼠标点 {moves} 个 · 折叠段 {groups} 段"
        )

    def _refresh_dirty(self) -> None:
        self.lbl_dirty.configure(text="● 未保存" if self.dirty else "")

    def _mark_dirty(self) -> None:
        self.dirty = True
        self._refresh_dirty()

    def expand_all(self, open_: bool) -> None:
        for gid in self._groups:
            self.tree.item(gid, open=open_)
        if open_:
            self._open_starts = {start for start, _end in self._groups.values()}
        else:
            self._open_starts.clear()

    # ------------------------------------------------------------------
    # 选择 / 编辑
    # ------------------------------------------------------------------
    def selected_indices(self) -> List[int]:
        indices = []
        for iid in self.tree.selection():
            if iid in self._groups:
                start, end = self._groups[iid]
                indices.extend(range(start, end))
            elif iid in self._row_event:
                indices.append(self._row_event[iid])
        return sorted(set(indices))

    def _on_double_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid in self._groups:
            opened = not self.tree.item(iid, "open")
            self.tree.item(iid, open=opened)
            count = self._groups[iid][1] - self._groups[iid][0]
            self.tree.item(iid, text=f"鼠标移动 · {count} 个点")
            self.tree.set(iid, "note", "已展开" if opened else "双击展开")
            start = self._groups[iid][0]
            if opened:
                self._open_starts.add(start)
            else:
                self._open_starts.discard(start)
            return
        if iid in self._row_event:
            self._edit_index(self._row_event[iid])

    def edit_selected(self) -> None:
        indices = self.selected_indices()
        if len(indices) == 1:
            self._edit_index(indices[0])
        elif not indices:
            messagebox.showinfo("编辑", "请先选中一条事件。", parent=self)
        else:
            messagebox.showinfo("编辑", "一次只能编辑一条事件。", parent=self)

    def _edit_index(self, index: int) -> None:
        if index < 0 or index >= len(self.events):
            return
        dialog = EventDialog(
            self.winfo_toplevel(), event=self.events[index], title="修改事件"
        )
        if dialog.result:
            self.update_event(index, dialog.result)

    def update_event(self, index: int, event: dict) -> None:
        if index < 0 or index >= len(self.events):
            return
        open_starts = self._currently_open()
        self._push_undo()
        self.events[index] = dict(event)
        self._rebuild(select_event=index, keep_open=open_starts)
        self._mark_dirty()

    def _currently_open(self) -> set:
        return {
            start for gid, (start, _end) in self._groups.items()
            if self.tree.item(gid, "open")
        }

    # ------------------------------------------------------------------
    # 插入 / 删除
    # ------------------------------------------------------------------
    def _insertion_point(self, before: bool) -> Tuple[int, Optional[int]]:
        """返回 (插入下标, 参照事件下标)。"""
        selection = self.tree.selection()
        if not selection:
            return (0 if before else len(self.events)), None

        iid = selection[0]
        if iid in self._groups:
            start, end = self._groups[iid]
            return (start if before else end), (start if before else end - 1)
        if iid in self._row_event:
            index = self._row_event[iid]
            return (index if before else index + 1), index
        return len(self.events), None

    def insert_event(self, before: bool = True, event: Optional[dict] = None) -> None:
        index, anchor = self._insertion_point(before)
        default_t = self._guess_time(index)
        if event is None:
            dialog = EventDialog(
                self.winfo_toplevel(), default_type="key_press",
                default_t=default_t, title="插入事件",
            )
            event = dialog.result
        if not event:
            return
        open_starts = self._currently_open()
        self._push_undo()
        self.events.insert(index, dict(event))
        self._rebuild(select_event=index, keep_open=open_starts)
        self._mark_dirty()

    def _guess_time(self, index: int) -> float:
        prev_t = None
        next_t = None
        for i in range(index - 1, -1, -1):
            prev_t = float(self.events[i].get("t", 0))
            break
        for i in range(index, len(self.events)):
            next_t = float(self.events[i].get("t", 0))
            break
        if prev_t is not None and next_t is not None:
            return round((prev_t + next_t) / 2, 3)
        if prev_t is not None:
            return round(prev_t + 0.1, 3)
        if next_t is not None:
            return round(max(0.0, next_t - 0.1), 3)
        return 0.0

    def delete_selected(self) -> None:
        indices = self.selected_indices()
        if not indices:
            messagebox.showinfo("删除", "请先选中要删除的事件。", parent=self)
            return
        if len(indices) > 1 and not messagebox.askyesno(
            "删除", f"确定删除选中的 {len(indices)} 条事件吗？", parent=self
        ):
            return
        self._push_undo()
        for index in sorted(indices, reverse=True):
            del self.events[index]
        self._open_starts.clear()
        self._rebuild()
        self._mark_dirty()

    # ------------------------------------------------------------------
    # 精简 / 撤销 / 源码
    # ------------------------------------------------------------------
    def simplify_moves(self) -> None:
        profile = motion.get_profile(
            next(
                (k for k, p in motion.PROFILES.items() if p.label == self.cmb_profile.get()),
                self._profile_key,
            )
        )
        filtered, stats = motion.filter_events(self.events, profile)
        if not stats.raw_moves:
            messagebox.showinfo("精简", "文件里没有鼠标移动事件。", parent=self)
            return
        if stats.kept_moves == stats.raw_moves:
            messagebox.showinfo("精简", "当前档位没有可再精简的鼠标点。", parent=self)
            return
        if not messagebox.askyesno(
            "精简鼠标移动",
            f"使用「{profile.label}」档：\n\n{stats.describe()}\n\n"
            f"事件总数 {len(self.events)} -> {len(filtered)}。\n"
            "此操作会修改内存中的内容，保存后才写入文件。是否继续？",
            parent=self,
        ):
            return
        self._push_undo()
        self.events = filtered
        self._open_starts.clear()
        self._rebuild()
        self._mark_dirty()
        log.info("界面精简[%s]：%s", profile.label, stats.describe())

    def _push_undo(self) -> None:
        self._undo.append(copy.deepcopy(self.events))
        if len(self._undo) > 30:
            self._undo.pop(0)

    def undo(self) -> None:
        if not self._undo:
            messagebox.showinfo("撤销", "没有可撤销的操作。", parent=self)
            return
        self.events = self._undo.pop()
        self._open_starts.clear()
        self._rebuild()
        self._mark_dirty()
        log.info("已撤销一步编辑")

    def show_source(self) -> None:
        if not self.path and not self.events:
            messagebox.showinfo("源码", "请先打开一个录制文件。", parent=self)
            return
        fmt = storage.detect_format(self.path or "x.json")
        payload = dict(self.payload or {})
        payload["events"] = self.events
        text = storage.dumps(payload, fmt=fmt)

        win = tk.Toplevel(self)
        win.title(f"源码预览 · {fmt.upper()}")
        win.geometry("900x600")
        frame = ttk.Frame(win, padding=6)
        frame.pack(fill="both", expand=True)
        text_widget = tk.Text(
            frame, wrap="none", font=("Consolas", 9), undo=False,
        )
        vsb = ttk.Scrollbar(frame, orient="vertical", command=text_widget.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=text_widget.xview)
        text_widget.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        text_widget.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text_widget.insert("1.0", text)
        text_widget.configure(state="disabled")
        ttk.Label(
            win, text="这是按当前编辑内容生成的源码（含未保存修改），只读。",
            style="Hint.TLabel", padding=6,
        ).pack(fill="x")
