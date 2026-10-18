"""把项目打包成可分发的 zip。

排除缓存、运行时配置和旧压缩包，保持目录结构；解压后直接
``python main.py`` 或双击 ``start_autocat.bat`` 即可运行。

    python tools/package_project.py
"""

from __future__ import annotations

import os
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from autocat import __version__  # noqa: E402

#: 这些目录 / 文件不打进包
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "dist", "build", ".pytest_cache"}
EXCLUDE_FILES = {"config.json", "schedules.json", "recording.json"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".zip"}
EXCLUDE_PREFIX = (".config-",)


def _is_personal_recording(name: str) -> bool:
    """个人录制（rec_*.json / rec_*.xml）不进开源包，演示文件保留。"""
    return name.startswith("rec_") and os.path.splitext(name)[1].lower() in {".json", ".xml"}


def main() -> int:
    root_name = f"AutoCat-{__version__}"
    output = os.path.join(BASE_DIR, f"{root_name}.zip")
    count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for folder, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for name in files:
                if name in EXCLUDE_FILES or name.startswith(EXCLUDE_PREFIX):
                    continue
                if _is_personal_recording(name):
                    continue
                if os.path.splitext(name)[1].lower() in EXCLUDE_SUFFIX:
                    continue
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                relative = os.path.relpath(path, BASE_DIR)
                archive.write(path, os.path.join(root_name, relative))
                count += 1
    size = os.path.getsize(output)
    print(f"已打包 {count} 个文件 -> {output} ({size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
