"""Windows 输入注入与权限诊断。

为什么不用 ``pynput.Controller`` 直接发送？因为它在 Windows 上调用
``SendInput`` 后完全不看返回值：当目标窗口以管理员权限运行时，Windows 的
UIPI（User Interface Privilege Isolation）会**静默丢弃**普通权限进程注入的
按键和点击。表现出来就是「回放跑到一半，鼠标还能动，但键盘和点击全没反应，
程序也不报错」——上次打开任务管理器后卡住就是这么来的。

这个模块做了两件事：

- 用 ``SendInput`` 自己发送并检查返回值，把「被拦截」变成可诊断的错误；
- 鼠标移动改用 ``SetCursorPos``（不受 UIPI 限制），并附带进程权限查询、
  前台窗口信息、以管理员身份重启等辅助函数。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("autocat.wininput")

# ---------------------------------------------------------------------------
# Win32 基础
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
)
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20

user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.SetCursorPos.restype = wintypes.BOOL

user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.GetCursorPos.restype = wintypes.BOOL

user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wintypes.HWND

user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetClassNameW.restype = ctypes.c_int

user32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
user32.OpenInputDesktop.restype = wintypes.HANDLE

user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
user32.CloseDesktop.restype = wintypes.BOOL

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

advapi32.OpenProcessToken.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
)
advapi32.OpenProcessToken.restype = wintypes.BOOL

advapi32.GetTokenInformation.argtypes = (
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
)
advapi32.GetTokenInformation.restype = wintypes.BOOL

_user32_send_input = user32.SendInput
_user32_send_input.argtypes = (wintypes.UINT, ctypes.c_void_p, ctypes.c_int)
_user32_send_input.restype = wintypes.UINT

# ---------------------------------------------------------------------------
# pynput 的 ctypes 结构（版本 1.8+），避免自己重新定义一遍出错
# ---------------------------------------------------------------------------
from pynput.keyboard._win32 import INPUT, INPUT_union, KEYBDINPUT, KeyCode  # noqa: E402
from pynput.mouse._win32 import MOUSEINPUT  # noqa: E402

WHEEL_DELTA = 120

#: 扩展键（右 Win、方向键等）需要 KEYEVENTF_EXTENDEDKEY 才能被正确识别
_EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # pgup pgdn end home arrows
    0x2D, 0x2E,                                        # insert delete
    0x5B, 0x5C,                                        # left/right win
    0x5D,                                              # apps
    0x6F,                                              # numpad divide
    0x90,                                              # num lock
    0xA3, 0xA5,                                        # right ctrl / right alt
}


def _key_params(key, press: bool) -> dict:
    """把 pynput 的 Key / KeyCode 转成 KEYBDINPUT 参数。"""
    base = getattr(key, "value", key)
    params = base._parameters(press)  # type: ignore[attr-defined]

    vk = int(params.get("wVk") or 0)
    if vk in _EXTENDED_VKS:
        params["dwFlags"] = int(params.get("dwFlags", 0)) | KEYBDINPUT.EXTENDEDKEY
    return params


# ---------------------------------------------------------------------------
# 权限 / 窗口诊断
# ---------------------------------------------------------------------------
def is_process_elevated(pid: Optional[int] = None) -> Optional[bool]:
    """查进程是否以管理员权限运行；查不到时返回 None。"""
    pid = os.getpid() if pid is None else int(pid)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            elevation = wintypes.DWORD()
            size = wintypes.DWORD(ctypes.sizeof(elevation))
            ok = advapi32.GetTokenInformation(
                token, TOKEN_ELEVATION, ctypes.byref(elevation),
                size, ctypes.byref(size),
            )
            if not ok:
                return None
            return bool(elevation.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def current_process_elevated() -> bool:
    return bool(is_process_elevated(os.getpid()))


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    exe: str
    elevated: Optional[bool]

    @property
    def label(self) -> str:
        return self.title or self.exe or self.class_name or f"进程 {self.pid}"


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _process_exe(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def foreground_window_info() -> Optional[WindowInfo]:
    """取当前前台窗口的进程、标题和权限。"""
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(
        hwnd=hwnd,
        pid=int(pid.value),
        title=_window_text(hwnd),
        class_name=_window_class(hwnd),
        exe=_process_exe(int(pid.value)),
        elevated=is_process_elevated(int(pid.value)),
    )


def secure_desktop_active() -> bool:
    """UAC 提示、锁屏等安全桌面是否正在占用输入。"""
    handle = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not handle:
        return True
    user32.CloseDesktop(handle)
    return False


def explain_failure(error_code: int, window: Optional[WindowInfo] = None) -> str:
    """把 Win32 错误码翻译成用户能看懂的说明。"""
    target = ""
    if window is not None:
        target = f"当前前台窗口「{window.label}」" + (
            "（以管理员权限运行）。" if window.elevated else "。"
        )
    if error_code == ERROR_ACCESS_DENIED:
        hint = (
            "Windows 拒绝向该窗口注入输入（UIPI 权限隔离）。"
            "请以管理员身份启动 AutoCat 后重试，或先切换到普通权限的窗口。"
        )
        return f"{target}{hint}" if target else hint
    if secure_desktop_active():
        return "当前处于 UAC / 锁屏等安全桌面，AutoCat 无法发送输入；请回到正常桌面后重试。"
    if error_code:
        return f"发送输入失败（Win32 错误 {error_code}）。"
    return "发送输入失败（系统未返回错误码）。"


def relaunch_as_admin(extra_args: Optional[list] = None) -> bool:
    """以管理员身份重新启动当前程序（会弹 UAC）。成功返回 True。"""
    exe = sys.executable
    if extra_args is None:
        script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
        args = [script, *sys.argv[1:]] if script else []
        parameters = subprocess.list2cmdline(args)
    else:
        parameters = subprocess.list2cmdline([str(a) for a in extra_args])

    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, parameters, os.getcwd(), 1
        )
        return int(result) > 32
    except Exception:
        log.exception("以管理员身份重启失败")
        return False


# ---------------------------------------------------------------------------
# 注入器
# ---------------------------------------------------------------------------
class WinInjector:
    """带返回值检查的输入注入器。"""

    name = "win32"

    def __init__(self) -> None:
        self.last_error = 0
        self.failed = 0
        self.succeeded = 0

    # -- 内部 ----------------------------------------------------------
    def _send(self, *inputs) -> bool:
        array = (INPUT * len(inputs))(*inputs)
        ctypes.set_last_error(0)
        sent = int(_user32_send_input(len(inputs), ctypes.byref(array), ctypes.sizeof(INPUT)))
        if sent != len(inputs):
            self.last_error = ctypes.get_last_error()
            self.failed += 1
            return False
        self.succeeded += 1
        return True

    @staticmethod
    def _keyboard_input(params: dict) -> "INPUT":
        return INPUT(type=INPUT.KEYBOARD, value=INPUT_union(ki=KEYBDINPUT(**params)))

    # -- 键盘 ----------------------------------------------------------
    def key(self, key, press: bool) -> bool:
        try:
            params = _key_params(key, press)
        except Exception:
            log.exception("无法解析按键 %r", key)
            self.last_error = ERROR_INVALID_PARAMETER
            self.failed += 1
            return False
        return self._send(self._keyboard_input(params))

    def press_key(self, key) -> bool:
        return self.key(key, True)

    def release_key(self, key) -> bool:
        return self.key(key, False)

    # -- 鼠标 ----------------------------------------------------------
    def move(self, x: int, y: int) -> bool:
        ctypes.set_last_error(0)
        if user32.SetCursorPos(int(x), int(y)):
            self.succeeded += 1
            return True
        self.last_error = ctypes.get_last_error()
        self.failed += 1
        return False

    def button(self, button, press: bool) -> bool:
        up_flag, down_flag, data = button.value
        flags = down_flag if press else up_flag
        value = INPUT_union(
            mi=MOUSEINPUT(dwFlags=flags, mouseData=int(data or 0))
        )
        return self._send(INPUT(type=INPUT.MOUSE, value=value))

    def scroll(self, dx: int, dy: int) -> bool:
        ok = True
        if dy:
            ok = self._send(
                INPUT(
                    type=INPUT.MOUSE,
                    value=INPUT_union(
                        mi=MOUSEINPUT(
                            dwFlags=MOUSEINPUT.WHEEL,
                            mouseData=int(dy * WHEEL_DELTA),
                        )
                    ),
                )
            ) and ok
        if dx:
            ok = self._send(
                INPUT(
                    type=INPUT.MOUSE,
                    value=INPUT_union(
                        mi=MOUSEINPUT(
                            dwFlags=MOUSEINPUT.HWHEEL,
                            mouseData=int(dx * WHEEL_DELTA),
                        )
                    ),
                )
            ) and ok
        return ok

    # -- 结果 ----------------------------------------------------------
    def take_error(self) -> int:
        error = self.last_error
        self.last_error = 0
        return error

    def status_line(self) -> str:
        return f"注入成功 {self.succeeded} 次，失败 {self.failed} 次"
