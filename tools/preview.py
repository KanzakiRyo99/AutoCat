"""启动一个用于观察/截图的 GUI 实例。

和 ``python main.py gui`` 的区别：可以选择默认标签页、自动加几条演示任务、
并改写窗口标题（方便截图脚本按标题定位，不会和正常实例混淆）。

    python tools/preview.py --tab 2 --title "AutoCat 预览"
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocat.gui import AutoCatGUI, enable_dpi_awareness  # noqa: E402
from autocat.scheduler import Task  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--title", default=None)
    parser.add_argument("--demo-tasks", action="store_true", help="加几条停用的演示任务")
    parser.add_argument("--file", default=None, help="录制内容标签页要打开的文件")
    parser.add_argument("--base", default=BASE_DIR)
    parser.add_argument("--local", action="store_true", help="任务写到工程目录（默认写临时目录）")
    args = parser.parse_args()

    base = args.base
    if args.demo_tasks and not args.local:
        # 用临时目录，避免污染工程里真正的 schedules.json
        import tempfile
        base = tempfile.mkdtemp(prefix="autocat_preview_")
        os.makedirs(os.path.join(base, "recordings"), exist_ok=True)
        for name in os.listdir(os.path.join(BASE_DIR, "recordings")):
            shutil.copy(os.path.join(BASE_DIR, "recordings", name),
                        os.path.join(base, "recordings", name))

    enable_dpi_awareness()
    root = tk.Tk()
    gui = AutoCatGUI(root, base)
    if args.title:
        root.title(args.title)

    if args.demo_tasks:
        demos = sorted(os.listdir(os.path.join(base, "recordings")))
        recipes = [
            dict(name="每天早上跑一遍", kind="daily", time_of_day="09:00", speed=1.0, loops=1),
            dict(name="半小时巡检", kind="interval", seconds=1800, speed=1.0, loops=1),
            dict(name="一分钟后提醒", kind="delay", seconds=60, speed=0.5, loops=2),
            dict(name="明天下午演示", kind="once", at="2026-10-17 15:30:00", speed=1.0, loops=1),
        ]
        for index, recipe in enumerate(recipes):
            if not demos:
                break
            task = Task(file=os.path.join(base, "recordings", demos[index % len(demos)]), **recipe)
            task.enabled = index != 3          # 留一条停用的，展示两种状态
            task.refresh_next()
            task.run_count = index
            gui.scheduler.tasks.append(task)   # 直接塞进列表，不写盘、不真的触发
        gui.refresh_tasks()

    if args.tab == 1:
        gui.nb.select(gui.tab_record)
    elif args.tab == 2:
        gui.nb.select(gui.tab_sched)
    else:
        target = args.file
        if not target:
            demos = sorted(
                (
                    os.path.join(base, "recordings", name)
                    for name in os.listdir(os.path.join(base, "recordings"))
                ),
                key=lambda p: os.path.getsize(p),
                reverse=True,
            )
            target = demos[0] if demos else None
        if target:
            gui.editor.load_path(target)
        gui.nb.select(gui.tab_editor)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
