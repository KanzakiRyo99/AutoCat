"""生成两个演示录制文件，方便第一次打开界面时看到东西。

生成的按键都是「无害」的：输入 hello、回车、滚动、Ctrl+S。
真正的回放只在你自己点「播放」时才会发生。

    python tools/make_demo.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocat import storage

REC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings")


def typing_demo() -> list:
    """点击一下，然后输入 hello 并回车。"""
    events = [
        {"t": 0.0, "type": "mouse_move", "x": 400, "y": 300},
        {"t": 0.15, "type": "mouse_click", "x": 400, "y": 300, "button": "left", "pressed": True},
        {"t": 0.22, "type": "mouse_click", "x": 400, "y": 300, "button": "left", "pressed": False},
    ]
    t = 0.6
    for ch in "hello":
        events.append({"t": round(t, 3), "type": "key_press", "key": ch})
        events.append({"t": round(t + 0.05, 3), "type": "key_release", "key": ch})
        t += 0.18
    events.append({"t": round(t, 3), "type": "key_press", "key": "enter"})
    events.append({"t": round(t + 0.05, 3), "type": "key_release", "key": "enter"})
    return events


def scroll_demo() -> list:
    """向下滚动两次，然后按一次 Ctrl+S。"""
    return [
        {"t": 0.0, "type": "mouse_scroll", "x": 600, "y": 400, "dx": 0, "dy": -3},
        {"t": 0.4, "type": "mouse_scroll", "x": 600, "y": 400, "dx": 0, "dy": -3},
        {"t": 0.9, "type": "key_press", "key": "ctrl_l"},
        {"t": 0.95, "type": "key_press", "key": "s"},
        {"t": 1.05, "type": "key_release", "key": "s"},
        {"t": 1.10, "type": "key_release", "key": "ctrl_l"},
    ]


def trajectory_demo() -> list:
    """多段鼠标轨迹 + 键盘/点击/滚轮，用来演示编辑器的折叠效果。"""
    import math

    events = []
    t = 0.0

    def move_to(x: int, y: int, step: float = 0.02) -> None:
        nonlocal t
        events.append({"t": round(t, 3), "type": "mouse_move", "x": int(x), "y": int(y)})
        t += step

    # 第一段：从左上往右下走一条折线
    for i in range(60):
        move_to(300 + i * 6, 250 + i * 3)
    events.append({"t": round(t, 3), "type": "mouse_click",
                   "x": 660, "y": 430, "button": "left", "pressed": True})
    t += 0.08
    events.append({"t": round(t, 3), "type": "mouse_click",
                   "x": 660, "y": 430, "button": "left", "pressed": False})
    t += 0.3

    # 中间：输入 hello
    for ch in "hello":
        events.append({"t": round(t, 3), "type": "key_press", "key": ch})
        events.append({"t": round(t + 0.04, 3), "type": "key_release", "key": ch})
        t += 0.16

    # 第二段：画一个半圆
    cx, cy = 900, 520
    for i in range(70):
        angle = math.pi * i / 69
        move_to(cx + math.cos(angle) * 160, cy - math.sin(angle) * 110, 0.018)
    events.append({"t": round(t, 3), "type": "mouse_scroll",
                   "x": 900, "y": 520, "dx": 0, "dy": -3})
    t += 0.35

    # 第三段：竖向移动
    for i in range(45):
        move_to(1000 + (i % 6) * 4, 300 + i * 6, 0.02)
    return events


def main() -> int:
    os.makedirs(REC_DIR, exist_ok=True)
    storage.save_recording(
        os.path.join(REC_DIR, "demo_输入hello.json"),
        typing_demo(),
        meta={"source": "demo", "note": "在焦点窗口输入 hello 并回车"},
    )
    storage.save_recording(
        os.path.join(REC_DIR, "demo_滚动并保存.xml"),
        scroll_demo(),
        meta={"source": "demo", "note": "向下滚动两次后按 Ctrl+S"},
    )
    storage.save_recording(
        os.path.join(REC_DIR, "demo_鼠标轨迹.json"),
        trajectory_demo(),
        meta={"source": "demo", "note": "多段鼠标轨迹，用于演示编辑器的折叠和精简"},
    )
    print("演示文件已生成到", REC_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
