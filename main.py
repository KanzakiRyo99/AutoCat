"""AutoCat 命令行入口。

用法：
    python main.py                        # 打开图形界面（默认）
    python main.py gui                    # 同上
    python main.py record -o rec.json     # 命令行录制（F9 开始 / F10 停止）
    python main.py play rec.json          # 回放
    python main.py play rec.json --speed 2 --loops 3
    python main.py list                   # 列出录制目录里的文件
    python main.py serve                  # 无界面常驻，按 schedules.json 定时执行
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import unicodedata

from autocat import __version__
from autocat.player import Player
from autocat.recorder import DEFAULT_START_KEY, DEFAULT_STOP_KEY, Recorder
from autocat.scheduler import Scheduler, Task
from autocat.storage import (
    FORMATS,
    describe,
    detect_format,
    format_duration,
    load_recording,
    save_recording,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REC_DIR = os.path.join(BASE_DIR, "recordings")
SCHEDULE_FILE = os.path.join(BASE_DIR, "schedules.json")


def setup_logging(debug: bool) -> None:
    # 控制台代码页不是 UTF-8 时（老终端/英文系统），中文日志会直接抛
    # UnicodeEncodeError，这里统一改成 UTF-8 + 替换非法字符。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s" if debug else "[%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_gui(args) -> int:
    from autocat import gui

    return gui.run(BASE_DIR)


def cmd_record(args) -> int:
    log = logging.getLogger("autocat")
    fmt = args.format or detect_format(args.output)

    log.info("AutoCat v%s 命令行录制", __version__)
    log.info("输出文件: %s", args.output)
    log.info("  %s 开始录制   %s 停止并保存   Ctrl+C 退出",
             DEFAULT_START_KEY.upper(), DEFAULT_STOP_KEY.upper())

    def on_stop(events):
        if not events:
            log.warning("没有录到事件，未保存")
            return
        save_recording(args.output, events, meta={"source": "cli"}, fmt=fmt)

    recorder = Recorder(
        on_stop=on_stop,
        start_key=args.start_key,
        stop_key=args.stop_key,
        record_moves=not args.no_moves,
        profile=args.mouse_profile,
    )
    try:
        recorder.run()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，退出")
    finally:
        recorder.shutdown()
    return 0


def cmd_play(args) -> int:
    log = logging.getLogger("autocat")
    if not os.path.exists(args.file):
        # 允许只写文件名，自动到录制目录里找
        candidate = os.path.join(REC_DIR, os.path.basename(args.file))
        if os.path.exists(candidate):
            args.file = candidate
        else:
            log.error("文件不存在: %s", args.file)
            return 2

    payload = load_recording(args.file)
    events = payload["events"]
    log.info("载入 %s：%d 条事件，原始时长 %.2fs",
             os.path.basename(args.file), len(events), describe(args.file)["duration"])

    player = Player(
        speed=args.speed,
        loops=args.loops,
        start_delay=args.delay,
        dry_run=args.dry_run,
    )
    try:
        result = player.play(events)
    except KeyboardInterrupt:
        player.abort()
        log.info("已中断")
        return 130
    if result.get("blocked"):
        log.error("回放被系统拦截：%s", result.get("reason") or "系统拒绝注入输入")
        log.info("请以管理员身份运行 AutoCat，或切换到普通权限的窗口后重试")
        return 3
    return 0 if not result.get("aborted") else 1


def _display_width(text: str) -> int:
    """算显示宽度：中日韩全角字符占两列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def _pad(text: str, width: int, right: bool = False) -> str:
    """按显示宽度补空格对齐（str.ljust 对中文会算错）。"""
    gap = " " * max(0, width - _display_width(text))
    return gap + str(text) if right else str(text) + gap


def cmd_list(args) -> int:
    directory = args.dir or REC_DIR
    if not os.path.isdir(directory):
        print(f"目录不存在: {directory}")
        return 1

    rows = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or os.path.splitext(name)[1].lower().lstrip(".") not in FORMATS:
            continue
        try:
            info = describe(path)
        except Exception as exc:
            print(f"  {name:<32} 读取失败: {exc}")
            continue
        rows.append((name, info["count"], format_duration(info["duration"]),
                     info["format"].upper(), info["created_at"].replace("T", " ")))

    if not rows:
        print(f"{directory} 里还没有录制文件")
        return 0

    print(f"{directory}\n")
    print(_pad("文件名", 34) + _pad("事件数", 8, right=True) + _pad("时长", 10, right=True)
          + _pad("格式", 7, right=True) + "  录制时间")
    print("-" * 84)
    for name, count, duration, fmt, created in rows:
        print(_pad(name, 34) + _pad(count, 8, right=True) + _pad(duration, 10, right=True)
              + _pad(fmt, 7, right=True) + "  " + created)
    return 0


def cmd_serve(args) -> int:
    """无界面常驻：按 schedules.json 定时回放。"""
    log = logging.getLogger("autocat")

    def runner(task: Task) -> None:
        if not task.file or not os.path.exists(task.file):
            log.error("任务「%s」的文件不存在: %s", task.name, task.file)
            return
        payload = load_recording(task.file)
        log.info("执行任务「%s」-> %s", task.name, os.path.basename(task.file))
        player = Player(
            speed=task.speed,
            loops=task.loops,
            start_delay=args.delay,
            dry_run=args.dry_run,
        )
        player.play(payload["events"])

    scheduler = Scheduler(runner=runner, path=args.file)
    scheduler.start()

    tasks = scheduler.list_tasks()
    if not tasks:
        log.warning("没有任何定时任务；请先在图形界面里添加，或编辑 %s", args.file)
    for task in tasks:
        log.info("  %-16s %-28s 下次 %s", task.name, task.describe(), task.next_run)

    log.info("常驻调度中，Ctrl+C 退出")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C")
    finally:
        scheduler.stop()
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autocat",
        description="AutoCat - 键鼠自动化工具（录制 / 回放 / 定时）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-V", "--version", action="version", version=f"AutoCat {__version__}")
    parser.add_argument("--debug", action="store_true", help="打印调试日志")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="打开图形界面（默认）")

    p_rec = sub.add_parser("record", help="命令行录制")
    p_rec.add_argument("-o", "--output", default=os.path.join(REC_DIR, "recording.json"),
                       help="输出文件（扩展名决定格式，默认 recording.json）")
    p_rec.add_argument("-f", "--format", choices=list(FORMATS), default=None,
                       help="强制保存格式")
    p_rec.add_argument("--start-key", default=DEFAULT_START_KEY, help="开始热键（默认 f9）")
    p_rec.add_argument("--stop-key", default=DEFAULT_STOP_KEY, help="停止热键（默认 f10）")
    p_rec.add_argument("--no-moves", action="store_true", help="不记录鼠标移动")
    p_rec.add_argument(
        "--mouse-profile", choices=["full", "standard", "compact"], default="standard",
        help="鼠标移动精简档：full 完整 / standard 标准（默认）/ compact 精简",
    )

    p_play = sub.add_parser("play", help="回放录制文件")
    p_play.add_argument("file", help="录制文件路径（也可以只写文件名）")
    p_play.add_argument("-s", "--speed", type=float, default=1.0, help="速度倍率，默认 1.0")
    p_play.add_argument("-l", "--loops", type=int, default=1, help="循环次数，0 表示无限")
    p_play.add_argument("-d", "--delay", type=float, default=1.0, help="开始前延时秒数，默认 1")
    p_play.add_argument("--dry-run", action="store_true", help="只走时序，不真的发送事件")

    p_list = sub.add_parser("list", help="列出录制文件")
    p_list.add_argument("--dir", default=None, help="录制目录，默认 ./recordings")

    p_serve = sub.add_parser("serve", help="无界面常驻，按计划执行任务")
    p_serve.add_argument("-f", "--file", default=SCHEDULE_FILE, help="任务文件，默认 schedules.json")
    p_serve.add_argument("-d", "--delay", type=float, default=1.0, help="每次执行前的延时秒数")
    p_serve.add_argument("--dry-run", action="store_true", help="只走时序，不真的发送事件")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "debug", False))

    command = args.command or "gui"
    handlers = {
        "gui": cmd_gui,
        "record": cmd_record,
        "play": cmd_play,
        "list": cmd_list,
        "serve": cmd_serve,
    }
    return handlers[command](args)


if __name__ == "__main__":
    sys.exit(main())
