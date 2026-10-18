"""AutoCat 录制模块。

职责：
- 全局监听键鼠事件（pynput）
- 通过热键或程序调用控制录制：F9 开始，F10 停止
- 把事件按相对时间戳保存成列表

两个容易被忽略但很重要的细节：

1. **忽略注入事件**：pynput 1.8+ 会告诉你某个事件是不是程序注入的。回放时
   自己发出的事件就是 injected，录制器默认丢弃它们，否则「边放边录」会把
   回放内容又录一遍，形成自我放大的死循环。
2. **热键本身不进录制**：按下开始热键时，该键（以及当时already按住的键）
   会被记入抑制表，它们的 release 不会被录进去。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, List, Optional, Tuple

from pynput import keyboard, mouse

from .motion import FilterStats, MouseProfile, filter_events, get_profile

log = logging.getLogger("autocat.recorder")

#: 默认热键（录制格式的按键名）
DEFAULT_START_KEY = "f9"
DEFAULT_STOP_KEY = "f10"


def _key_to_str(key) -> str:
    """把 pynput 的按键对象转成可序列化的字符串。"""
    if isinstance(key, keyboard.Key):
        return key.name  # 'enter', 'ctrl_l', 'f5' ...
    if isinstance(key, keyboard.KeyCode):
        if key.char is not None:
            c = key.char
            # Windows 下 Ctrl+字母 会返回控制字符 \x01-\x1a，映射回 a-z
            if len(c) == 1 and 1 <= ord(c) <= 26:
                return chr(ord(c) + 96)
            return c  # 'a', 'A', '1' ...
        if key.vk is not None:
            return f"vk:{key.vk}"  # 兜底
    return str(key)


class Recorder:
    """键鼠录制器。线程安全，监听器运行在 pynput 自己的线程里。

    典型用法（GUI）：``rec.start()`` 之后监听一直开着，随时 ``start_recording()``
    / ``stop_recording()``；退出时 ``shutdown()``。
    """

    def __init__(
        self,
        on_stop: Optional[Callable[[List[dict]], None]] = None,
        on_start: Optional[Callable[[], None]] = None,
        move_threshold: Optional[int] = None,
        record_moves: bool = True,
        start_key: str = DEFAULT_START_KEY,
        stop_key: str = DEFAULT_STOP_KEY,
        hotkeys_enabled: bool = True,
        ignore_injected: bool = True,
        mouse_grace: float = 0.25,
        profile: MouseProfile | str | None = None,
    ) -> None:
        """
        参数：
            on_stop: 停止录制时的回调，参数是事件列表（可能为空）
            on_start: 开始录制时的回调
            move_threshold: 鼠标移动降噪阈值；None 表示用 ``profile`` 里的值
            record_moves: 是否记录鼠标移动（关掉可让文件小很多）
            start_key / stop_key: 起止热键名
            hotkeys_enabled: 是否响应热键（回放期间可临时关掉）
            ignore_injected: 是否忽略程序注入的事件
            mouse_grace: 开始录制后多少秒内忽略鼠标事件，用于滤掉「点按钮」本身
            profile: 鼠标精简预设（见 :mod:`autocat.motion`），停止录制时生效
        """
        self.events: List[dict] = []
        self.recording: bool = False

        self.on_stop = on_stop
        self.on_start = on_start
        self.record_moves = record_moves
        self.profile: MouseProfile = get_profile(profile)
        self.move_threshold = (
            int(move_threshold) if move_threshold is not None else self.profile.min_distance
        )
        self.start_key = start_key
        self.stop_key = stop_key
        self.hotkeys_enabled = hotkeys_enabled
        self.ignore_injected = ignore_injected
        self.mouse_grace = mouse_grace

        self._t0: Optional[float] = None
        self._pressed: set = set()
        self._suppressed: set = set()
        self._last_move_pos: Optional[tuple] = None
        self._last_move_time: float = 0.0
        self._grace_until: float = 0.0
        self._lock = threading.RLock()
        self._ignore_rect: Optional[Tuple[int, int, int, int]] = None

        # 悬浮窗要展示的实时状态（由监听线程更新，主线程只读）
        self.live_pos: Optional[Tuple[int, int]] = None
        self.live_keys: deque = deque(maxlen=6)
        self.live_events: int = 0
        self.last_filter_stats: Optional[FilterStats] = None

        self._kb_listener: Optional[keyboard.Listener] = None
        self._ms_listener: Optional[mouse.Listener] = None

    # ------------------------------------------------------------------
    # 内部：事件写入
    # ------------------------------------------------------------------
    def _append(self, etype: str, **fields) -> None:
        if self._t0 is None:
            return
        t = time.perf_counter() - self._t0
        event = {"t": round(t, 4), "type": etype, **fields}
        with self._lock:
            self.events.append(event)
            self.live_events += 1
        log.debug("[%8.4f] %-12s %s", t, etype, fields)

    # ------------------------------------------------------------------
    # 开始 / 停止录制
    # ------------------------------------------------------------------
    def start_recording(self, mouse_grace: Optional[float] = None) -> bool:
        """开始录制。已经在录时返回 False。"""
        with self._lock:
            if self.recording:
                return False
            grace = self.mouse_grace if mouse_grace is None else mouse_grace
            self.events = []
            self._t0 = time.perf_counter()
            self._last_move_pos = None
            self._last_move_time = 0.0
            self._grace_until = self._t0 + max(0.0, grace)
            self.live_pos = None
            self.live_keys.clear()
            self.live_events = 0
            self.last_filter_stats = None
            # 把开始热键本身（以及当时按住的其他键）标记为抑制，避免录进去
            self._suppressed = set(self._pressed)
            self.recording = True
        log.info(">>> 录制中 <<<  按 %s 停止", self.stop_key.upper())
        if self.on_start:
            try:
                self.on_start()
            except Exception:
                log.exception("on_start 回调执行失败")
        return True

    def stop_recording(self) -> List[dict]:
        """停止录制、精简鼠标移动并返回事件列表。没在录时返回空列表。"""
        with self._lock:
            if not self.recording:
                return []
            self.recording = False
            events = list(self.events)
            elapsed = time.perf_counter() - self._t0 if self._t0 else 0.0
            self._t0 = None
            self._suppressed.clear()

        # 停止后再做路径简化：录制过程中保持原始轨迹，避免影响实时状态
        filtered, stats = filter_events(events, self.profile)
        self.last_filter_stats = stats
        self.events = filtered
        if stats.raw_moves:
            log.info("鼠标精简[%s]：%s", self.profile.label, stats.describe())

        log.info("<<< 录制结束 >>> %d 条事件，时长 %.2f 秒", len(filtered), elapsed)
        if self.on_stop:
            try:
                self.on_stop(filtered)
            except Exception:
                log.exception("on_stop 回调执行失败")
        return filtered

    def cancel_recording(self) -> None:
        """放弃当前录制（不保存、不触发 on_stop）。"""
        with self._lock:
            if not self.recording:
                return
            self.recording = False
            self.events = []
            self._t0 = None
            self._suppressed.clear()
            self.live_pos = None
            self.live_keys.clear()
            self.live_events = 0
        log.warning("录制已取消，内容未保存")

    def recording_elapsed(self) -> float:
        with self._lock:
            if not self.recording or self._t0 is None:
                return 0.0
            return max(0.0, time.perf_counter() - self._t0)

    def snapshot(self) -> dict:
        """给悬浮窗用的只读快照。"""
        with self._lock:
            return {
                "recording": self.recording,
                "elapsed": (time.perf_counter() - self._t0) if (self.recording and self._t0) else 0.0,
                "pos": self.live_pos,
                "keys": list(self.live_keys),
                "events": self.live_events,
            }

    def set_ignore_rect(self, rect: Optional[Tuple[int, int, int, int]]) -> None:
        """忽略落在该矩形（屏幕坐标）内的鼠标事件，用于悬浮窗自身。"""
        with self._lock:
            self._ignore_rect = tuple(int(v) for v in rect) if rect else None

    def _in_ignore_rect(self, x: int, y: int) -> bool:
        rect = self._ignore_rect
        if not rect:
            return False
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    # 兼容旧名字
    _start = start_recording
    _stop = stop_recording

    # ------------------------------------------------------------------
    # 键盘事件
    # ------------------------------------------------------------------
    def _on_press(self, key, injected: bool = False) -> None:
        if injected and self.ignore_injected:
            return
        key_str = _key_to_str(key)
        with self._lock:
            self._pressed.add(key_str)
            if self.recording:
                self.live_keys.append(key_str)

        if self.hotkeys_enabled and key_str == self.start_key:
            if not self.recording:
                log.info("[热键] %s -> 开始录制", key_str.upper())
                self.start_recording(mouse_grace=0.0)
            return

        if self.hotkeys_enabled and key_str == self.stop_key:
            if self.recording:
                log.info("[热键] %s -> 停止录制", key_str.upper())
                self.stop_recording()
            return

        with self._lock:
            suppressed = key_str in self._suppressed
        if suppressed:
            return

        if self.recording:
            self._append("key_press", key=key_str)

    def _on_release(self, key, injected: bool = False) -> None:
        if injected and self.ignore_injected:
            return
        key_str = _key_to_str(key)
        with self._lock:
            self._pressed.discard(key_str)
            suppressed = key_str in self._suppressed
            if suppressed:
                self._suppressed.discard(key_str)

        if suppressed:
            return
        if self.recording:
            self._append("key_release", key=key_str)

    # ------------------------------------------------------------------
    # 鼠标事件
    # ------------------------------------------------------------------
    def _mouse_ready(self) -> bool:
        return self.recording and time.perf_counter() >= self._grace_until

    def _on_move(self, x: int, y: int, injected: bool = False) -> None:
        if injected and self.ignore_injected:
            return
        if self.recording:
            with self._lock:
                self.live_pos = (x, y)
        if not self._mouse_ready() or not self.record_moves:
            return
        if self._in_ignore_rect(x, y):
            return
        if self._last_move_pos is not None:
            dx = abs(x - self._last_move_pos[0])
            dy = abs(y - self._last_move_pos[1])
            if dx + dy < self.move_threshold:
                return
        self._last_move_pos = (x, y)
        self._append("mouse_move", x=x, y=y)

    def _on_click(self, x: int, y: int, button, pressed: bool, injected: bool = False) -> None:
        if injected and self.ignore_injected:
            return
        if self.recording:
            with self._lock:
                self.live_pos = (x, y)
        if not self._mouse_ready() or self._in_ignore_rect(x, y):
            return
        self._append(
            "mouse_click",
            x=x,
            y=y,
            button=button.name,
            pressed=pressed,
        )

    def _on_scroll(self, x: int, y: int, dx: int, dy: int, injected: bool = False) -> None:
        if injected and self.ignore_injected:
            return
        if self.recording:
            with self._lock:
                self.live_pos = (x, y)
        if not self._mouse_ready() or self._in_ignore_rect(x, y):
            return
        self._append("mouse_scroll", x=x, y=y, dx=dx, dy=dy)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动监听（非阻塞）。重复调用是安全的。"""
        if self._kb_listener is not None and self._kb_listener.is_alive():
            return
        self._kb_listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._ms_listener = mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        self._kb_listener.start()
        self._ms_listener.start()
        log.debug("键鼠监听已启动")

    def wait(self) -> None:
        """阻塞，直到监听器停止。"""
        while (
            self._kb_listener is not None
            and self._ms_listener is not None
            and self._kb_listener.is_alive()
            and self._ms_listener.is_alive()
        ):
            time.sleep(0.1)

    def run(self) -> None:
        """启动监听并阻塞（命令行模式）。"""
        self.start()
        log.info(
            "监听已启动：%s 开始录制，%s 停止并保存",
            self.start_key.upper(), self.stop_key.upper(),
        )
        self.wait()

    def stop(self) -> None:
        """关闭监听器。"""
        for listener in (self._kb_listener, self._ms_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        self._kb_listener = None
        self._ms_listener = None

    def shutdown(self) -> None:
        """停止录制（如果正在进行）并关闭监听器。"""
        if self.recording:
            self.stop_recording()
        self.stop()
