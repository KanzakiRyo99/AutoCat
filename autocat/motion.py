"""鼠标移动记录的精简策略。

一段鼠标移动在录制文件里通常是成百上千个 ``mouse_move`` 事件，而其中大部分
点的信息量很低（直线滑动的中间点、肉眼看不出的抖动）。这里提供两种手段：

1. **录制时采样**：相邻两点距离小于 ``min_distance`` 或时间间隔小于
   ``min_interval`` 的点直接丢弃 —— 从源头减少事件。
2. **路径简化**：对每一段连续的鼠标移动跑一次 Douglas–Peucker（RDP），
   在 ``simplify`` 像素误差内用折线近似原轨迹，只保留端点与拐点。

两者叠加后，典型录制的鼠标点数能降到原来的 10%–25%，而回放轨迹肉眼看不出差别。
文件格式完全不变（仍然是普通 ``mouse_move`` 事件），所以 JSON/XML 和旧版本
相互兼容。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class MouseProfile:
    """一档鼠标记录精度。"""

    key: str
    label: str
    min_distance: int
    min_interval: float
    simplify: float
    description: str


#: 三档预设。``standard`` 是默认值：轨迹基本无损，体积约为原始的 1/3。
PROFILES: Dict[str, MouseProfile] = {
    "full": MouseProfile(
        key="full",
        label="完整",
        min_distance=1,
        min_interval=0.0,
        simplify=0.0,
        description="每个移动点都记录，文件最大，适合研究轨迹或需要逐帧还原的场景。",
    ),
    "standard": MouseProfile(
        key="standard",
        label="标准",
        min_distance=2,
        min_interval=0.012,
        simplify=1.2,
        description="滤掉抖动并按 1.2 像素误差简化路径，体积约为完整档的 1/3，推荐。",
    ),
    "compact": MouseProfile(
        key="compact",
        label="精简",
        min_distance=4,
        min_interval=0.03,
        simplify=3.0,
        description="只保留明显的移动方向变化，体积约为完整档的 1/4，适合长录制。",
    ),
}

#: 默认预设
DEFAULT_PROFILE = "standard"

PROFILE_LABELS = {p.label: p for p in PROFILES.values()}
PROFILE_KEYS = tuple(PROFILES)


def get_profile(name: str | MouseProfile | None) -> MouseProfile:
    """按 key / 中文标签 / 对象取预设，无法识别时回落到默认档。"""
    if isinstance(name, MouseProfile):
        return name
    text = str(name or "").strip()
    if text in PROFILES:
        return PROFILES[text]
    if text in PROFILE_LABELS:
        return PROFILE_LABELS[text]
    return PROFILES[DEFAULT_PROFILE]


def simplify_indices(points: Sequence[Tuple[float, float]], epsilon: float) -> List[int]:
    """Douglas–Peucker：返回需要保留的点下标（首尾一定保留）。

    ``epsilon`` 是允许的最大垂直偏差（像素）。``<=0`` 表示不简化。
    """
    count = len(points)
    if epsilon <= 0 or count <= 2:
        return list(range(count))

    keep = [False] * count
    keep[0] = keep[-1] = True
    stack = [(0, count - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue

        ax, ay = points[start]
        bx, by = points[end]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)

        farthest = -1.0
        farthest_index = -1
        for index in range(start + 1, end):
            px, py = points[index]
            if length == 0:
                distance = math.hypot(px - ax, py - ay)
            else:
                # 点到直线 ab 的垂直距离（叉积 / 底边长）
                distance = abs(dy * px - dx * py + bx * ay - by * ax) / length
            if distance > farthest:
                farthest = distance
                farthest_index = index

        if farthest > epsilon:
            keep[farthest_index] = True
            stack.append((start, farthest_index))
            stack.append((farthest_index, end))

    return [index for index, flag in enumerate(keep) if flag]


@dataclass
class FilterStats:
    """精简前后的统计，用于日志与界面提示。"""

    raw_moves: int = 0
    kept_moves: int = 0
    groups: int = 0

    @property
    def removed(self) -> int:
        return max(0, self.raw_moves - self.kept_moves)

    @property
    def ratio(self) -> float:
        return (self.kept_moves / self.raw_moves) if self.raw_moves else 1.0

    def describe(self) -> str:
        if not self.raw_moves:
            return "没有鼠标移动记录"
        return (
            f"鼠标移动 {self.raw_moves} -> {self.kept_moves} 个点"
            f"（保留 {self.ratio * 100:.0f}%，共 {self.groups} 段）"
        )


def filter_events(events: Iterable[dict], profile: MouseProfile) -> Tuple[List[dict], FilterStats]:
    """按预设过滤事件列表，返回 ``(新事件列表, 统计)``。

    只处理 ``mouse_move``；连续的移动会被视为一段，段与段之间以任何非移动事件
    分隔。其余的键盘 / 点击 / 滚轮事件原样保留，顺序不变。
    """
    stats = FilterStats()
    result: List[dict] = []
    run: List[dict] = []
    last_pos: Tuple[int, int] | None = None
    last_time: float | None = None

    def flush() -> None:
        if not run:
            return
        stats.groups += 1
        points = [(float(e.get("x", 0)), float(e.get("y", 0))) for e in run]
        indices = simplify_indices(points, profile.simplify)
        for index in indices:
            result.append(run[index])
        stats.kept_moves += len(indices)
        run.clear()

    for event in events:
        if event.get("type") != "mouse_move":
            flush()
            last_pos = None
            last_time = None
            result.append(event)
            continue

        stats.raw_moves += 1
        try:
            x = int(event.get("x", 0))
            y = int(event.get("y", 0))
            t = float(event.get("t", 0.0))
        except (TypeError, ValueError):
            continue

        if last_pos is not None:
            if abs(x - last_pos[0]) + abs(y - last_pos[1]) < profile.min_distance:
                continue
            if profile.min_interval and last_time is not None:
                if t - last_time < profile.min_interval:
                    continue

        run.append(event)
        last_pos = (x, y)
        last_time = t

    flush()
    return result, stats
