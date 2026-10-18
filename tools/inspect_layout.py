"""不依赖截图地检查 GUI 布局：打印各区域的实际高度和 y 坐标。

截图能看出「好不好看」，但看不出「为什么」，这个脚本用量化数据回答后者。

    python tools/inspect_layout.py
"""

from __future__ import annotations

import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocat.gui import AutoCatGUI, enable_dpi_awareness  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    gui = AutoCatGUI(root, BASE_DIR)

    # 让几何管理器跑完
    for _ in range(12):
        root.update()

    print(f"root           : {root.winfo_width()}x{root.winfo_height()}")
    print(f"要求的窗口尺寸 : {root.winfo_reqwidth()}x{root.winfo_reqheight()}")
    print()
    print(f"{'区域':<22}{'y':>6}{'高':>7}{'需求高':>8}  可见")
    print("-" * 56)

    regions = [
        ("工具条", gui.btn_record.master),
        ("标签页 Notebook", gui.nb),
        ("运行日志", gui.log_text.master),
    ]
    for label, widget in regions:
        visible = widget.winfo_ismapped()
        print(f"{label:<22}{widget.winfo_y():>6}{widget.winfo_height():>7}"
              f"{widget.winfo_reqheight():>8}  {'是' if visible else '否'}")

    print()
    print("各标签页需求高度:")
    for tab_id, name in (
        (gui.tab_record, "录制与回放"),
        (gui.tab_sched, "定时任务"),
        (gui.tab_editor, "录制内容"),
    ):
        print(f"  {name:<12} 需求 {tab_id.winfo_reqheight():>4}px  实际 {tab_id.winfo_height():>4}px")

    ok = gui.log_text.master.winfo_ismapped() and gui.log_text.master.winfo_height() > 40
    print()
    print("结论:", "日志面板可见" if ok else "!! 日志面板被挤出窗口，需要调整布局")

    gui.shutdown()
    root.destroy()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
