"""AutoCat 定时任务模块。

支持四种触发方式：

======  ==========================================================
once    在指定时刻执行一次（ISO 时间字符串）
delay   从现在起 N 秒后执行一次
interval 每 N 秒执行一次
daily   每天 HH:MM 执行一次
======  ==========================================================

调度器本身不关心「执行什么」，只接受一个 ``runner(task)`` 回调 —— 这样它既能
被 GUI 用，也能被命令行用，还方便单测。所有任务跑在唯一的工作线程里，天然
串行，不会出现两个回放抢键鼠的情况。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Callable, List, Optional

log = logging.getLogger("autocat.scheduler")

KINDS = ("once", "delay", "interval", "daily")

KIND_LABELS = {
    "once": "指定时刻一次",
    "delay": "延时一次",
    "interval": "固定间隔",
    "daily": "每天定时",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _parse_dt(text: str) -> Optional[datetime]:
    """尽量宽容地解析时间字符串。"""
    if not text:
        return None
    text = str(text).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%H:%M:%S",
        "%H:%M",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        # 只有时分的情况补上今天
        if fmt in ("%H:%M:%S", "%H:%M"):
            now = datetime.now()
            parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
        return parsed
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_time_of_day(text: str) -> Optional[tuple]:
    """把 ``"08:30"`` 解析成 ``(8, 30)``。"""
    parsed = _parse_dt(text)
    if parsed is None:
        return None
    return parsed.hour, parsed.minute, parsed.second


def _fmt(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
@dataclass
class Task:
    """一条定时任务。"""

    name: str
    file: str = ""
    kind: str = "interval"
    at: str = ""            # once 用
    seconds: int = 3600     # delay / interval 用
    time_of_day: str = "09:00"  # daily 用
    speed: float = 1.0
    loops: int = 1
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    run_count: int = 0
    anchor: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    # ------------------------------------------------------------------
    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def describe(self) -> str:
        """人能看懂的触发规则描述。"""
        if self.kind == "once":
            return f"{self.at or '未设置'} 执行一次"
        if self.kind == "delay":
            return f"延时 {self.seconds}s 后执行一次"
        if self.kind == "interval":
            return f"每 {self.seconds}s 执行一次"
        if self.kind == "daily":
            return f"每天 {self.time_of_day} 执行"
        return self.kind

    # ------------------------------------------------------------------
    def compute_next(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """计算下一次执行时间；返回 None 表示不会再执行。"""
        now = now or datetime.now()

        if self.kind == "once":
            target = _parse_dt(self.at)
            if target is None or self.last_run:
                return None
            return target if target > now else None

        if self.kind == "delay":
            if self.last_run:
                return None
            base = _parse_dt(self.anchor) or now
            return base + timedelta(seconds=max(1, int(self.seconds)))

        if self.kind == "interval":
            base = _parse_dt(self.last_run) or _parse_dt(self.anchor) or now
            nxt = base + timedelta(seconds=max(1, int(self.seconds)))
            # 机器休眠/长时间未运行导致错过很多次时，不要补跑，直接推到未来
            if nxt <= now:
                missed = int((now - nxt).total_seconds() // max(1, int(self.seconds))) + 1
                nxt += timedelta(seconds=missed * max(1, int(self.seconds)))
            return nxt

        if self.kind == "daily":
            tod = _parse_time_of_day(self.time_of_day)
            if tod is None:
                return None
            hour, minute, second = tod
            nxt = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            return nxt

        return None

    def refresh_next(self, now: Optional[datetime] = None) -> None:
        self.next_run = _fmt(self.compute_next(now)) if self.enabled else None

    def is_due(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled or not self.next_run:
            return False
        target = _parse_dt(self.next_run)
        return bool(target and target <= (now or datetime.now()))


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------
class Scheduler:
    """后台轮询式调度器。

    参数：
        runner: ``runner(task)``，在调度器的工作线程里被调用
        path: 任务持久化文件，None 表示不持久化
        poll: 轮询间隔（秒）
        on_change: 任务列表变化时的回调，供界面刷新
    """

    def __init__(
        self,
        runner: Optional[Callable[[Task], None]] = None,
        path: Optional[str] = None,
        poll: float = 0.5,
        on_change: Optional[Callable[[], None]] = None,
        on_fire: Optional[Callable[[Task], None]] = None,
    ) -> None:
        self.runner = runner
        self.path = path
        self.poll = poll
        self.on_change = on_change
        self.on_fire = on_fire

        self.tasks: List[Task] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._work_queue: "queue.Queue[Task]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self.busy_task: Optional[str] = None

        if path and os.path.exists(path):
            self.load()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        self._worker = threading.Thread(target=self._work_loop, name="autocat-runner", daemon=True)
        self._worker.start()

        with self._lock:
            now = datetime.now()
            for task in self.tasks:
                task.refresh_next(now)

        self._thread = threading.Thread(target=self._tick_loop, name="autocat-scheduler", daemon=True)
        self._thread.start()
        log.info("调度器已启动，共 %d 条任务", len(self.tasks))

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._thread, self._worker):
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
        self._thread = None
        self._worker = None
        log.info("调度器已停止")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    def _tick_loop(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self._tick()
            except Exception:
                log.exception("调度轮询出错")

    def _tick(self) -> None:
        now = datetime.now()
        fired = []
        with self._lock:
            for task in self.tasks:
                if not task.enabled:
                    continue
                if not task.next_run:
                    task.refresh_next(now)
                    continue
                if task.is_due(now):
                    task.last_run = _fmt(now)
                    task.run_count += 1
                    # 先算下一次，再交给工作线程，避免重复触发
                    nxt = task.compute_next(now)
                    task.next_run = _fmt(nxt)
                    if nxt is None and task.kind in ("once", "delay"):
                        task.enabled = False
                    fired.append(task)

        for task in fired:
            log.info("触发定时任务「%s」(%s)", task.name, task.describe())
            if self.on_fire:
                try:
                    self.on_fire(task)
                except Exception:
                    log.exception("on_fire 回调失败")
            self._work_queue.put(task)

        if fired:
            self.save()
            if self.on_change:
                try:
                    self.on_change()
                except Exception:
                    pass

    def _work_loop(self) -> None:
        while True:
            try:
                task = self._work_queue.get(timeout=0.3)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue

            self.busy_task = task.id
            try:
                if self.runner:
                    self.runner(task)
                else:
                    log.warning("没有配置 runner，任务「%s」被跳过", task.name)
            except Exception:
                log.exception("定时任务「%s」执行失败", task.name)
            finally:
                self.busy_task = None
                self._work_queue.task_done()

    # ------------------------------------------------------------------
    # 任务管理
    # ------------------------------------------------------------------
    def add(self, task: Task) -> Task:
        with self._lock:
            task.refresh_next()
            self.tasks.append(task)
        log.info("添加定时任务「%s」：%s，下次 %s", task.name, task.describe(), task.next_run)
        self.save()
        self._notify_change()
        return task

    def create(self, **kwargs) -> Task:
        """按关键字参数建任务并加入调度。"""
        return self.add(Task(**kwargs))

    def remove(self, task_id: str) -> bool:
        with self._lock:
            before = len(self.tasks)
            self.tasks = [t for t in self.tasks if t.id != task_id]
            removed = len(self.tasks) != before
        if removed:
            log.info("已删除定时任务 %s", task_id)
            self.save()
            self._notify_change()
        return removed

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            for task in self.tasks:
                if task.id == task_id:
                    return task
        return None

    def set_enabled(self, task_id: str, enabled: bool) -> bool:
        task = self.get(task_id)
        if not task:
            return False
        with self._lock:
            task.enabled = enabled
            if enabled:
                task.last_run = None if task.kind in ("once", "delay") else task.last_run
                task.refresh_next()
            else:
                task.next_run = None
        log.info("%s定时任务「%s」", "启用" if enabled else "停用", task.name)
        self.save()
        self._notify_change()
        return True

    def run_now(self, task_id: str) -> bool:
        """立即手动执行一次（不影响下次计划）。"""
        task = self.get(task_id)
        if not task:
            return False
        log.info("手动执行定时任务「%s」", task.name)
        self._work_queue.put(task)
        return True

    def list_tasks(self) -> List[Task]:
        with self._lock:
            return list(self.tasks)

    def _notify_change(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def save(self) -> None:
        if not self.path:
            return
        try:
            with self._lock:
                data = [t.to_dict() for t in self.tasks]
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            log.exception("保存定时任务失败: %s", self.path)

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self.tasks = [Task.from_dict(item) for item in data]
            log.info("已载入 %d 条定时任务: %s", len(self.tasks), self.path)
        except Exception:
            log.exception("载入定时任务失败: %s", self.path)
