"""AutoCat 配置（config.json）。

所有可调项集中在这里，缺省值写在 :data:`DEFAULTS`；读取时做深合并，
所以旧版本留下的残缺配置也能用，未知字段会原样保留。

配置文件放在工程根目录（和 schedules.json 同级），删除即恢复默认。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from typing import Any, Optional

log = logging.getLogger("autocat.config")

#: 默认配置。新增字段时直接加在这里，老配置文件会自动补上。
DEFAULTS: dict = {
    # 鼠标记录预设：full / standard / compact（见 autocat.motion）
    "mouse_profile": "standard",
    "record_moves": True,
    "overlay": {
        "enabled": True,
        # top_left / top_right
        "corner": "top_right",
        "alpha": 0.92,
        "text_color": "#d9ffd9",
        "show_keys": 3,
    },
    "playback": {
        # 输入被系统拦截时：stop = 立即停止并提示；skip = 跳过该事件继续
        "on_input_blocked": "stop",
        # 回放结束时是否统计、提示被跳过的事件
        "report_failures": True,
    },
}

#: 半透明悬浮窗可选位置
OVERLAY_CORNERS = {
    "top_left": "左上角",
    "top_right": "右上角",
}

#: 输入被拦截时的策略
BLOCKED_POLICIES = {
    "stop": "停止回放并提示",
    "skip": "跳过被拦截的事件，继续回放",
}


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 合并进 base（递归），返回新 dict。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """config.json 的读写封装，支持 ``config.get("overlay.alpha")``。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self.data: dict = copy.deepcopy(DEFAULTS)
        if path:
            self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("配置文件根节点必须是对象")
            self.data = _deep_merge(DEFAULTS, raw)
            log.debug("已载入配置: %s", self.path)
        except Exception:
            log.exception("读取配置失败，使用默认值: %s", self.path)

    def save(self) -> None:
        if not self.path:
            return
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            # 先写临时文件再替换，避免写到一半把配置写坏
            fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=parent or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except Exception:
            log.exception("保存配置失败: %s", self.path)

    # ------------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        """按 ``"overlay.alpha"`` 这种点号路径取值。"""
        node: Any = self.data
        for part in str(dotted).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        """按点号路径写值并立即落盘。"""
        parts = str(dotted).split(".")
        node = self.data
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
        self.save()

    # ------------------------------------------------------------------
    def section(self, name: str) -> dict:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Config(path={self.path!r}, data={self.data!r})"
