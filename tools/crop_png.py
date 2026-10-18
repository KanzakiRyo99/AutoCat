"""用 tkinter 自带的 PhotoImage 裁剪 PNG（不需要 Pillow）。

    python tools/crop_png.py 输入.png 输出.png x1 y1 x2 y2
"""

from __future__ import annotations

import sys
import tkinter as tk


def main(argv: list) -> int:
    if len(argv) != 7:
        print(__doc__)
        return 2
    src, dst, x1, y1, x2, y2 = argv[1], argv[2], *(int(v) for v in argv[3:7])

    root = tk.Tk()
    root.withdraw()
    image = tk.PhotoImage(file=src)
    print(f"源图尺寸: {image.width()}x{image.height()}")

    x2 = min(x2, image.width())
    y2 = min(y2, image.height())
    crop = tk.PhotoImage(width=max(1, x2 - x1), height=max(1, y2 - y1))
    # Tk 8.6 的 image copy 支持 -from，可以按像素区域裁剪
    crop.tk.call(crop, "copy", image, "-from", x1, y1, x2, y2, "-to", 0, 0)
    crop.write(dst, format="png")
    print(f"已裁剪: {dst} ({crop.width()}x{crop.height()})")
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
