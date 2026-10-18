"""演示完整的「开始录制 → 主窗口最小化 → 悬浮窗」流程。

只往录制器里喂几个假事件，不真的监听、也不真的发送键鼠；不会保存录制文件。
适合观察悬浮窗布局和截图。12 秒后自动取消录制并退出。

    python tools/preview_overlay.py
"""

from __future__ import annotations

import argparse
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pynput import keyboard  # noqa: E402

from autocat.gui import AutoCatGUI, enable_dpi_awareness  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--opaque", action="store_true", help="截图时使用不透明背景")
    args = parser.parse_args()

    enable_dpi_awareness()
    root = tk.Tk()
    gui = AutoCatGUI(root, BASE_DIR)

    gui.config.set("overlay.corner", "top_right")
    if args.opaque:
        gui.config.set("overlay.alpha", 1.0)
    gui.overlay.apply_config()
    gui.overlay.win.title("AutoCatOverlayDebug")

    # 走真实入口：会最小化主窗口，并通过队列让悬浮窗出现
    gui.start_recording()

    def feed() -> None:
        gui.recorder._on_move(812, 604)
        for key in (keyboard.Key.ctrl_l, keyboard.KeyCode.from_char("s")):
            gui.recorder._on_press(key)

    root.after(500, feed)

    def done() -> None:
        gui.cancel_recording()
        gui.shutdown()
        root.destroy()

    root.after(12000, done)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
