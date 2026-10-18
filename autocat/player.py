"""AutoCat 回放模块。

职责：
- 把事件列表按原始相对时间（可变速）回放到系统
- 支持循环、播放前延时、中止热键、干跑（dry-run，不真的发事件）
- 回放被中止或异常退出时，释放所有仍按下的键 / 鼠标按钮，避免「卡键」

关于 ``injected``：
pynput 1.8+ 的回调会带上「该事件是否由程序注入」的标志。回放自己产生的事件
会被标为 injected，因此中止热键监听器会忽略这类事件 —— 否则一段录制里只要
含有中止键（例如 F12），回放就会把自己中断。
"""

from __future__ import annotations

import logging
import threading
import time
from functools import lru_cache
from typing import Callable, Iterable, List, Optional, Sequence

from pynput import keyboard, mouse

from .storage import recording_duration
from .wininput import WinInjector, explain_failure, foreground_window_info

log = logging.getLogger("autocat.player")


class PlaybackAborted(Exception):
    """回放被用户/热键中止。"""


class InputBlocked(RuntimeError):
    """系统拒绝注入输入（通常是目标窗口权限更高）。"""


# ---------------------------------------------------------------------------
# 按键字符串 -> pynput 按键对象
# ---------------------------------------------------------------------------
#: 录制时可能出现的别名（不同平台/版本的写法），回放时统一映射
_KEY_ALIASES = {
    "escape": "esc",
    "return": "enter",
    "control": "ctrl",
    "del": "delete",
    "pgup": "page_up",
    "pgdn": "page_down",
    "win": "cmd",
    "super": "cmd",
    "win_l": "cmd_l",
    "win_r": "cmd_r",
    "capslock": "caps_lock",
    "numlock": "num_lock",
    "printscreen": "print_screen",
}


@lru_cache(maxsize=512)
def key_from_str(name: str):
    """把录制文件里的按键字符串还原成 pynput 可发送的对象。"""
    name = str(name)
    if name.startswith("vk:"):
        try:
            return keyboard.KeyCode.from_vk(int(name[3:]))
        except ValueError:
            log.warning("无法解析虚拟键码: %s", name)
            return None

    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)

    if hasattr(keyboard.Key, name):
        return getattr(keyboard.Key, name)

    alias = _KEY_ALIASES.get(name)
    if alias and hasattr(keyboard.Key, alias):
        return getattr(keyboard.Key, alias)

    log.warning("未知按键 %r，按普通字符处理", name)
    return keyboard.KeyCode.from_char(name)


_BUTTONS = {
    "left": mouse.Button.left,
    "right": mouse.Button.right,
    "middle": mouse.Button.middle,
    "x1": mouse.Button.x1,
    "x2": mouse.Button.x2,
}


def button_from_str(name: str):
    return _BUTTONS.get(str(name).lower(), mouse.Button.left)


class _NullInjector:
    """干跑模式用的空注入器：什么都不发，但永远成功。"""

    name = "dry-run"
    failed = 0
    succeeded = 0

    def move(self, x, y):
        return True

    def press_key(self, key):
        return True

    def release_key(self, key):
        return True

    def button(self, button, press):
        return True

    def scroll(self, dx, dy):
        return True

    def take_error(self):
        return 0


# ---------------------------------------------------------------------------
# 回放器
# ---------------------------------------------------------------------------
class Player:
    """把事件列表回放到系统。一次只允许一个回放在跑。"""

    def __init__(
        self,
        speed: float = 1.0,
        loops: int = 1,
        abort_keys: Sequence[str] = ("f12",),
        dry_run: bool = False,
        start_delay: float = 0.0,
        on_state: Optional[Callable[[str, dict], None]] = None,
        on_event: Optional[Callable[[int, dict], None]] = None,
        injector=None,
        on_input_blocked: str = "stop",
    ) -> None:
        """
        参数：
            speed: 速度倍率，2.0 = 两倍速，0.5 = 半速
            loops: 循环次数，0 或负数表示无限循环
            abort_keys: 中止热键（录制格式的按键名，如 ``"f12"``、``"esc"``）
            dry_run: 干跑模式，只走时序不真的发送事件（自测用）
            start_delay: 开始前等待秒数，留出切换到目标窗口的时间
            on_state: 状态回调 ``(state, info)``，state ∈ started/finished/aborted/
                blocked/error
            on_event: 每条事件回放后的回调 ``(index, event)``
            injector: 注入器，默认是带错误检查的 :class:`~autocat.wininput.WinInjector`；
                测试可传入假对象
            on_input_blocked: ``"stop"`` 遇到输入被系统拦截时立即停止并提示；
                ``"skip"`` 跳过失败的事件继续
        """
        self.speed = float(speed) if speed else 1.0
        self.loops = int(loops)
        self.abort_keys = {str(k).lower() for k in abort_keys}
        self.dry_run = bool(dry_run)
        self.start_delay = max(0.0, float(start_delay))
        self.on_state = on_state
        self.on_event = on_event
        self.on_input_blocked = str(on_input_blocked or "stop").lower()
        self._injector = injector
        if self.dry_run and self._injector is None:
            self._injector = _NullInjector()

        self._abort = threading.Event()
        self._lock = threading.Lock()
        self._running = False

        self._held_keys: List = []
        self._held_buttons: List = []
        self._failed_events = 0
        self._blocked_reason = ""

        self._listener: Optional[keyboard.Listener] = None
        self._play_thread: Optional[threading.Thread] = None
        self.last_result: dict = {}

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._running

    def _emit(self, state: str, **info) -> None:
        if self.on_state:
            try:
                self.on_state(state, info)
            except Exception:
                log.exception("on_state 回调失败")

    # ------------------------------------------------------------------
    # 中止
    # ------------------------------------------------------------------
    def abort(self) -> None:
        """请求中止当前回放（可以从任意线程调用）。"""
        self._abort.set()

    def _on_abort_key(self, key, injected: bool = False) -> None:
        if injected:  # 忽略回放自身注入的事件，避免自我中止
            return
        try:
            name = key.name if isinstance(key, keyboard.Key) else getattr(key, "char", None)
        except Exception:
            return
        if name and str(name).lower() in self.abort_keys:
            log.info("[热键] %s -> 中止回放", name)
            self.abort()

    def _start_abort_listener(self) -> None:
        if self.dry_run or not self.abort_keys:
            return
        self._listener = keyboard.Listener(on_press=self._on_abort_key)
        self._listener.start()

    def _stop_abort_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    # ------------------------------------------------------------------
    # 时序
    # ------------------------------------------------------------------
    def _wait(self, seconds: float) -> None:
        """可被中止打断的精确等待。"""
        if seconds <= 0:
            return
        deadline = time.perf_counter() + seconds
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            if remaining > 0.003:
                # Event.wait 比 sleep 更容易被及时唤醒
                if self._abort.wait(min(remaining - 0.002, 0.02)):
                    raise PlaybackAborted()
            else:
                # 最后几毫秒自旋，绕开 Windows 定时器 ~15ms 的精度限制
                while time.perf_counter() < deadline:
                    if self._abort.is_set():
                        raise PlaybackAborted()

    # ------------------------------------------------------------------
    # 事件派发
    # ------------------------------------------------------------------
    def _ensure_injector(self):
        if self._injector is None:
            self._injector = WinInjector()
        return self._injector

    def _dispatch(self, event: dict) -> bool:
        """发送一条事件，返回是否成功。"""
        injector = self._injector
        etype = event.get("type")

        if etype == "key_press":
            key = key_from_str(event.get("key", ""))
            if key is None:
                return True
            if not injector.press_key(key):
                return False
            self._held_keys.append(key)
            return True

        if etype == "key_release":
            key = key_from_str(event.get("key", ""))
            if key is None:
                return True
            ok = injector.release_key(key)
            # 释放成功才从「仍按住」里移除；失败时保留，收尾时再重试一次
            if ok:
                for i in range(len(self._held_keys) - 1, -1, -1):
                    if self._held_keys[i] == key:
                        del self._held_keys[i]
                        break
            return ok

        if etype == "mouse_move":
            return injector.move(int(event.get("x", 0)), int(event.get("y", 0)))

        if etype == "mouse_click":
            ok = injector.move(int(event.get("x", 0)), int(event.get("y", 0)))
            button = button_from_str(event.get("button", "left"))
            pressed = bool(event.get("pressed", True))
            ok = injector.button(button, pressed) and ok
            if pressed:
                if ok:
                    self._held_buttons.append(button)
            elif ok:
                for i in range(len(self._held_buttons) - 1, -1, -1):
                    if self._held_buttons[i] == button:
                        del self._held_buttons[i]
                        break
            return ok

        if etype == "mouse_scroll":
            ok = True
            if "x" in event and "y" in event:
                ok = injector.move(int(event["x"]), int(event["y"]))
            ok = injector.scroll(int(event.get("dx", 0)), int(event.get("dy", 0))) and ok
            return ok

        log.warning("未知事件类型: %r，已跳过", etype)
        return True

    def _release_all(self) -> None:
        """释放所有仍按下的键与鼠标按钮（防卡键）。"""
        if self.dry_run or self._injector is None:
            self._held_keys.clear()
            self._held_buttons.clear()
            return
        for key in reversed(self._held_keys):
            try:
                self._injector.release_key(key)
            except Exception:
                pass
        for button in reversed(self._held_buttons):
            try:
                self._injector.button(button, False)
            except Exception:
                pass
        self._held_keys.clear()
        self._held_buttons.clear()

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def play(self, events: Iterable[dict]) -> dict:
        """阻塞回放事件列表，返回结果 dict。"""
        with self._lock:
            if self._running:
                raise RuntimeError("已有回放在进行中")
            self._running = True
        try:
            return self._play(list(events))
        finally:
            with self._lock:
                self._running = False

    def play_async(self, events: Iterable[dict]) -> threading.Thread:
        """在后台线程回放，返回该线程。"""
        events = list(events)
        thread = threading.Thread(target=self._guarded_play, args=(events,), daemon=True)
        self._play_thread = thread
        thread.start()
        return thread

    def _guarded_play(self, events: List[dict]) -> None:
        """线程入口：吃掉正在播放时的异常，避免线程里抛栈。"""
        try:
            self.play(events)
        except RuntimeError as exc:
            log.warning("%s", exc)

    def _play(self, events: List[dict]) -> dict:
        self._abort.clear()
        self._failed_events = 0
        self._blocked_reason = ""

        total = len(events)
        plan = recording_duration(events)
        if not events:
            log.warning("事件列表为空，没有可回放的内容")
            self.last_result = {
                "played": 0, "total": 0, "elapsed": 0.0,
                "aborted": False, "blocked": False, "failed": 0, "reason": "",
            }
            self._emit("finished", played=0, elapsed=0.0)
            return self.last_result

        injector = None if self.dry_run else self._ensure_injector()
        log.info(
            "开始回放：%d 条事件，原始时长 %.2fs，速度 %.2fx，循环 %s%s",
            total, plan, self.speed,
            "无限" if self.loops <= 0 else self.loops,
            "（干跑）" if self.dry_run else "",
        )
        self._emit("started", total=total, duration=plan, speed=self.speed, loops=self.loops)

        played = 0
        failed = 0
        aborted = False
        blocked = False
        started_at = time.perf_counter()
        self._start_abort_listener()

        try:
            if self.start_delay > 0:
                log.info("%.1fs 后开始（F12 可取消）", self.start_delay)
                self._wait(self.start_delay)

            loop_index = 0
            while True:
                loop_index += 1
                if self.loops > 0 and loop_index > self.loops:
                    break
                if loop_index > 1:
                    log.info("第 %d 轮回放", loop_index)

                base = time.perf_counter()
                for index, event in enumerate(events):
                    if self._abort.is_set():
                        raise PlaybackAborted()
                    self._wait(float(event.get("t", 0.0)) / self.speed
                               - (time.perf_counter() - base))
                    if not self.dry_run:
                        if not self._dispatch(event):
                            failed += 1
                            self._failed_events += 1
                            error = 0
                            if hasattr(injector, "take_error"):
                                error = int(injector.take_error() or 0)
                            if not self._blocked_reason:
                                window = foreground_window_info()
                                self._blocked_reason = explain_failure(error, window)
                                log.error("输入被系统拦截：%s", self._blocked_reason)
                            if self.on_input_blocked == "skip":
                                if failed <= 5:
                                    log.warning(
                                        "跳过被拦截的事件 #%d %s（Win32 错误 %s）",
                                        index, event.get("type"), error,
                                    )
                                if self.on_event:
                                    try:
                                        self.on_event(index, event)
                                    except Exception:
                                        log.exception("on_event 回调失败")
                                continue
                            raise InputBlocked(self._blocked_reason)
                    played += 1
                    if self.on_event:
                        try:
                            self.on_event(index, event)
                        except Exception:
                            log.exception("on_event 回调失败")
                # 无限循环时回到 while 顶部继续下一轮（中止检查在轮首）

        except InputBlocked as exc:
            aborted = True
            blocked = True
            log.error("回放已停止：%s", exc)
        except PlaybackAborted:
            aborted = True
            log.warning("回放已中止（已播放 %d 条）", played)
        except Exception:
            log.exception("回放出错")
            self._emit("error", played=played)
            raise
        finally:
            self._release_all()
            self._stop_abort_listener()

        elapsed = time.perf_counter() - started_at
        state = "blocked" if blocked else ("aborted" if aborted else "finished")
        self._emit(
            state, played=played, elapsed=elapsed,
            failed=failed, reason=self._blocked_reason,
        )

        if not aborted:
            if failed:
                log.warning("回放结束：%d 条事件，其中 %d 条被系统拦截；耗时 %.2fs",
                            played, failed, elapsed)
            else:
                log.info("回放结束：%d 条事件，耗时 %.2fs", played, elapsed)

        self.last_result = {
            "played": played,
            "total": total,
            "elapsed": elapsed,
            "aborted": aborted,
            "blocked": blocked,
            "failed": failed,
            "reason": self._blocked_reason,
        }
        return self.last_result
