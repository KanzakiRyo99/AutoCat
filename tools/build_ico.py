"""把 tools/make_icon.ps1 生成的多尺寸 PNG 打包成 Windows .ico。

只依赖标准库：ICO 允许在 Vista+ 的图标目录里直接嵌入 PNG 数据，
所以不需要额外的图像库。

    python tools/build_ico.py
"""

from __future__ import annotations

import os
import struct

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(BASE_DIR, "assets")
SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_ico() -> str:
    images = []
    for size in SIZES:
        path = os.path.join(ASSETS, f"autocat_{size}.png")
        with open(path, "rb") as f:
            images.append((size, f.read()))

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = []
    for size, data in images:
        width = 0 if size >= 256 else size
        height = 0 if size >= 256 else size
        entries.append(
            struct.pack(
                "<BBBBHHII",
                width, height, 0, 0, 1, 32, len(data), offset,
            )
        )
        offset += len(data)

    ico_path = os.path.join(ASSETS, "autocat.ico")
    with open(ico_path, "wb") as f:
        f.write(header)
        for entry in entries:
            f.write(entry)
        for _size, data in images:
            f.write(data)
    return ico_path


def main() -> int:
    path = build_ico()
    # 256px PNG 也复制一份，方便 README / 未来做托盘图标
    src = os.path.join(ASSETS, "autocat_256.png")
    dst = os.path.join(ASSETS, "autocat.png")
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(data)
    print(f"已生成 {path} ({os.path.getsize(path)} bytes)")
    print(f"已生成 {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
