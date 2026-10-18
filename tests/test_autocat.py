"""AutoCat 核心模块自测。

这些测试完全不发送真实的键鼠事件：
- 回放统一用 ``dry_run=True``（只走时序）
- 录制器直接调用内部回调模拟按键，不启动监听

运行：
    python -m unittest discover -s tests -v
    或直接：  python tests/test_autocat.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pynput import keyboard  # noqa: E402

from autocat import config as config_module  # noqa: E402
from autocat import motion, storage  # noqa: E402
from autocat.player import Player, key_from_str  # noqa: E402
from autocat.recorder import Recorder  # noqa: E402
from autocat.scheduler import Scheduler, Task  # noqa: E402


SAMPLE_EVENTS = [
    {"t": 0.0, "type": "key_press", "key": "a"},
    {"t": 0.1, "type": "key_release", "key": "a"},
    {"t": 0.2, "type": "mouse_move", "x": 100, "y": 200},
    {"t": 0.25, "type": "mouse_click", "x": 100, "y": 200, "button": "left", "pressed": True},
    {"t": 0.3, "type": "mouse_click", "x": 100, "y": 200, "button": "left", "pressed": False},
    {"t": 0.4, "type": "mouse_scroll", "x": 100, "y": 200, "dx": 0, "dy": -1},
    {"t": 0.5, "type": "key_press", "key": "vk:81"},
]


class TempDirMixin:
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()


# ---------------------------------------------------------------------------
class TestStorage(TempDirMixin, unittest.TestCase):
    def test_json_roundtrip(self):
        path = os.path.join(self.tmp, "a.json")
        storage.save_recording(path, SAMPLE_EVENTS, meta={"source": "test"})
        payload = storage.load_recording(path)
        self.assertEqual(len(payload["events"]), len(SAMPLE_EVENTS))
        self.assertEqual(payload["meta"]["source"], "test")
        # 类型要保持：bool 还是 bool，int 还是 int
        click = payload["events"][3]
        self.assertIs(click["pressed"], True)
        self.assertIsInstance(click["x"], int)
        self.assertIsInstance(payload["events"][0]["t"], float)

    def test_xml_roundtrip(self):
        path = os.path.join(self.tmp, "a.xml")
        storage.save_recording(path, SAMPLE_EVENTS, meta={"source": "test"})
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("<?xml"))
        self.assertIn("<event ", text)

        payload = storage.load_recording(path)
        self.assertEqual(len(payload["events"]), len(SAMPLE_EVENTS))
        click = payload["events"][3]
        self.assertIs(click["pressed"], True)
        self.assertEqual(click["button"], "left")
        self.assertIsInstance(click["x"], int)

    def test_json_xml_equivalence(self):
        """同一份数据转成两种格式后，读回来应该完全一致。"""
        jpath = os.path.join(self.tmp, "b.json")
        xpath = os.path.join(self.tmp, "b.xml")
        storage.save_recording(jpath, SAMPLE_EVENTS)
        storage.save_recording(xpath, SAMPLE_EVENTS)
        self.assertEqual(storage.load_recording(jpath)["events"],
                         storage.load_recording(xpath)["events"])

    def test_format_detection_and_helpers(self):
        self.assertEqual(storage.detect_format("x.XML"), "xml")
        self.assertEqual(storage.detect_format("x.txt"), "json")
        self.assertEqual(storage.detect_format("x.txt", default=""), "")
        self.assertEqual(storage.format_duration(75), "01:15")
        self.assertEqual(storage.format_duration(3725), "1:02:05")
        self.assertEqual(storage.recording_duration(SAMPLE_EVENTS), 0.5)

    def test_describe(self):
        path = os.path.join(self.tmp, "c.json")
        storage.save_recording(path, SAMPLE_EVENTS)
        info = storage.describe(path)
        self.assertEqual(info["count"], len(SAMPLE_EVENTS))
        self.assertEqual(info["format"], "json")
        self.assertGreater(info["size"], 0)

    def test_events_sorted_by_time(self):
        path = os.path.join(self.tmp, "d.json")
        storage.save_recording(path, list(reversed(SAMPLE_EVENTS)))
        times = [e["t"] for e in storage.load_recording(path)["events"]]
        self.assertEqual(times, sorted(times))


# ---------------------------------------------------------------------------
class TestPlayer(TempDirMixin, unittest.TestCase):
    def test_key_mapping(self):
        self.assertEqual(key_from_str("a"), keyboard.KeyCode.from_char("a"))
        self.assertEqual(key_from_str("enter"), keyboard.Key.enter)
        self.assertEqual(key_from_str("esc"), keyboard.Key.esc)
        self.assertEqual(key_from_str("vk:81"), keyboard.KeyCode.from_vk(81))
        # 别名
        self.assertEqual(key_from_str("escape"), keyboard.Key.esc)
        self.assertEqual(key_from_str("return"), keyboard.Key.enter)

    def test_dry_run_timing(self):
        player = Player(speed=1.0, loops=1, dry_run=True)
        start = time.perf_counter()
        result = player.play(SAMPLE_EVENTS)
        elapsed = time.perf_counter() - start
        self.assertFalse(result["aborted"])
        self.assertEqual(result["played"], len(SAMPLE_EVENTS))
        self.assertLess(abs(elapsed - 0.5), 0.08, f"时序偏差过大: {elapsed:.3f}s")

    def test_speed_scaling(self):
        player = Player(speed=2.0, loops=1, dry_run=True)
        start = time.perf_counter()
        player.play(SAMPLE_EVENTS)
        elapsed = time.perf_counter() - start
        self.assertLess(abs(elapsed - 0.25), 0.08, f"2 倍速耗时异常: {elapsed:.3f}s")

    def test_loops(self):
        player = Player(speed=10.0, loops=3, dry_run=True)
        result = player.play(SAMPLE_EVENTS)
        self.assertEqual(result["played"], len(SAMPLE_EVENTS) * 3)

    def test_empty_events(self):
        result = Player(dry_run=True).play([])
        self.assertEqual(result["played"], 0)
        self.assertFalse(result["aborted"])

    def test_abort(self):
        player = Player(speed=1.0, dry_run=True, start_delay=0.0)
        # 造一个很长的录制，然后在 0.2 秒后中止
        long_events = [{"t": i * 0.1, "type": "mouse_move", "x": i, "y": i} for i in range(200)]

        import threading
        threading.Timer(0.2, player.abort).start()

        start = time.perf_counter()
        result = player.play(long_events)
        elapsed = time.perf_counter() - start
        self.assertTrue(result["aborted"])
        self.assertLess(elapsed, 1.0, "中止响应太慢")
        self.assertLess(result["played"], len(long_events))

    def test_concurrent_play_rejected(self):
        player = Player(dry_run=True)
        player._running = True
        with self.assertRaises(RuntimeError):
            player.play(SAMPLE_EVENTS)
        player._running = False

    def test_state_callbacks(self):
        states = []
        player = Player(speed=100.0, dry_run=True, on_state=lambda s, i: states.append(s))
        player.play(SAMPLE_EVENTS)
        self.assertEqual(states[0], "started")
        self.assertEqual(states[-1], "finished")


# ---------------------------------------------------------------------------
class TestRecorder(unittest.TestCase):
    def _make(self, **kwargs):
        self.stopped = []
        self.started = []
        rec = Recorder(
            on_stop=lambda events: self.stopped.append(events),
            on_start=lambda: self.started.append(True),
            **kwargs,
        )
        self.addCleanup(rec.stop)
        return rec

    def test_programmatic_record(self):
        rec = self._make()
        self.assertTrue(rec.start_recording(mouse_grace=0.0))
        self.assertFalse(rec.start_recording(), "重复开始应该返回 False")

        rec._on_press(keyboard.KeyCode.from_char("a"))
        rec._on_release(keyboard.KeyCode.from_char("a"))
        rec._on_click(10, 20, type("B", (), {"name": "left"})(), True)

        events = rec.stop_recording()
        self.assertEqual([e["type"] for e in events],
                         ["key_press", "key_release", "mouse_click"])
        self.assertEqual(events[0]["key"], "a")
        self.assertEqual(events[2]["button"], "left")
        self.assertTrue(self.stopped, "on_stop 回调没被调用")
        self.assertTrue(rec.events == events)

    def test_hotkey_starts_and_stops(self):
        rec = self._make()
        rec._on_press(keyboard.Key.f9)          # 开始
        self.assertTrue(rec.recording)
        rec._on_release(keyboard.Key.f9)        # 开始热键本身不该被录进去

        rec._on_press(keyboard.KeyCode.from_char("b"))
        rec._on_release(keyboard.KeyCode.from_char("b"))

        rec._on_press(keyboard.Key.f10)         # 停止
        self.assertFalse(rec.recording)

        events = self.stopped[0]
        self.assertEqual([e["key"] for e in events], ["b", "b"])

    def test_hotkeys_can_be_disabled(self):
        rec = self._make(hotkeys_enabled=False)
        rec._on_press(keyboard.Key.f9)
        self.assertFalse(rec.recording, "关掉热键后 F9 不该开始录制")

    def test_injected_events_ignored(self):
        rec = self._make(ignore_injected=True)
        rec.start_recording()
        rec._on_press(keyboard.KeyCode.from_char("x"), True)   # injected
        rec._on_release(keyboard.KeyCode.from_char("x"), True)
        self.assertEqual(rec.stop_recording(), [])

    def test_injected_events_recorded_when_allowed(self):
        rec = self._make(ignore_injected=False)
        rec.start_recording()
        rec._on_press(keyboard.KeyCode.from_char("x"), True)
        self.assertEqual(len(rec.stop_recording()), 1)

    def test_mouse_grace_suppresses_button_click(self):
        rec = self._make(mouse_grace=0.3)
        rec.start_recording()
        rec._on_click(5, 5, type("B", (), {"name": "left"})(), False)
        self.assertEqual(rec.events, [], "宽限期内不该记录鼠标事件")

        time.sleep(0.35)
        rec._on_click(5, 5, type("B", (), {"name": "left"})(), True)
        self.assertEqual(len(rec.events), 1)

    def test_hotkey_start_has_no_mouse_grace(self):
        rec = self._make(mouse_grace=0.5)
        rec._on_press(keyboard.Key.f9)
        rec._on_click(5, 5, type("B", (), {"name": "left"})(), True)
        self.assertEqual(len(rec.events), 1, "热键开始不该有鼠标宽限期")

    def test_move_threshold(self):
        rec = self._make(move_threshold=10)
        rec.start_recording(mouse_grace=0.0)
        rec._on_move(100, 100)
        rec._on_move(102, 101)      # 距离 3 < 10，丢弃
        rec._on_move(200, 200)      # 距离够大，记录
        moves = [e for e in rec.events if e["type"] == "mouse_move"]
        self.assertEqual(len(moves), 2)

    def test_record_moves_toggle(self):
        rec = self._make(record_moves=False)
        rec.start_recording(mouse_grace=0.0)
        rec._on_move(100, 100)
        rec._on_move(300, 300)
        self.assertEqual(rec.events, [])

    def test_control_char_normalised(self):
        rec = self._make()
        rec.start_recording()
        rec._on_press(keyboard.KeyCode.from_char("\x03"))  # Ctrl+C
        self.assertEqual(rec.events[0]["key"], "c")

    def test_special_key_names(self):
        rec = self._make()
        rec.start_recording()
        rec._on_press(keyboard.Key.enter)
        rec._on_press(keyboard.KeyCode.from_vk(81))
        keys = [e["key"] for e in rec.events]
        self.assertEqual(keys, ["enter", "vk:81"])


# ---------------------------------------------------------------------------
class TestScheduler(TempDirMixin, unittest.TestCase):
    def test_compute_next_once(self):
        now = datetime(2030, 1, 1, 12, 0, 0)
        task = Task(name="t", kind="once", at="2030-01-01 13:00:00")
        self.assertEqual(task.compute_next(now), datetime(2030, 1, 1, 13, 0, 0))

        past = Task(name="t", kind="once", at="2020-01-01 13:00:00")
        self.assertIsNone(past.compute_next(now), "过去的时刻不该再触发")

    def test_compute_next_delay(self):
        now = datetime(2030, 1, 1, 12, 0, 0)
        task = Task(name="t", kind="delay", seconds=60, anchor=now.isoformat())
        self.assertEqual(task.compute_next(now), datetime(2030, 1, 1, 12, 1, 0))
        task.last_run = now.isoformat()
        self.assertIsNone(task.compute_next(now), "延时任务只跑一次")

    def test_compute_next_interval_skips_missed(self):
        now = datetime(2030, 1, 1, 12, 0, 0)
        # 锚点在 1 小时前，间隔 60 秒 —— 不该补跑 60 次，应直接推到一个未来的整点
        task = Task(name="t", kind="interval", seconds=60,
                    anchor=(now - timedelta(hours=1)).isoformat())
        nxt = task.compute_next(now)
        self.assertGreater(nxt, now)
        self.assertLessEqual((nxt - now).total_seconds(), 60)

    def test_compute_next_daily(self):
        now = datetime(2030, 1, 1, 12, 0, 0)
        task = Task(name="t", kind="daily", time_of_day="09:30")
        self.assertEqual(task.compute_next(now), datetime(2030, 1, 2, 9, 30, 0))

        task2 = Task(name="t", kind="daily", time_of_day="18:00")
        self.assertEqual(task2.compute_next(now), datetime(2030, 1, 1, 18, 0, 0))

    def test_describe(self):
        self.assertIn("每 60s", Task(name="t", kind="interval", seconds=60).describe())
        self.assertIn("每天 09:00", Task(name="t", kind="daily", time_of_day="09:00").describe())

    def test_persistence(self):
        path = os.path.join(self.tmp, "sched.json")
        sched = Scheduler(path=path)
        sched.create(name="任务A", file="a.json", kind="interval", seconds=120)
        sched.create(name="任务B", file="b.json", kind="daily", time_of_day="08:00")

        again = Scheduler(path=path)
        self.assertEqual(len(again.list_tasks()), 2)
        self.assertEqual({t.name for t in again.list_tasks()}, {"任务A", "任务B"})
        self.assertTrue(all(t.next_run for t in again.list_tasks()))

    def test_enable_disable(self):
        sched = Scheduler()
        task = sched.create(name="t", kind="interval", seconds=60)
        self.assertTrue(task.enabled)
        sched.set_enabled(task.id, False)
        self.assertIsNone(task.next_run)
        sched.set_enabled(task.id, True)
        self.assertIsNotNone(task.next_run)

    def test_remove(self):
        sched = Scheduler()
        task = sched.create(name="t", kind="interval", seconds=60)
        self.assertTrue(sched.remove(task.id))
        self.assertFalse(sched.remove(task.id))
        self.assertEqual(sched.list_tasks(), [])

    def test_task_actually_fires(self):
        """端到端：调度器真的会在到点时调用 runner。"""
        fired = []
        done = __import__("threading").Event()

        def runner(task):
            fired.append(task.name)
            done.set()

        sched = Scheduler(runner=runner, poll=0.05)
        task = sched.create(name="快跑", kind="delay", seconds=1)
        task.anchor = (datetime.now() - timedelta(seconds=2)).isoformat()
        task.refresh_next()
        sched.start()
        self.addCleanup(sched.stop)

        self.assertTrue(done.wait(3.0), "任务没有在 3 秒内触发")
        self.assertEqual(fired, ["快跑"])
        # 延时任务跑完应自动停用
        self.assertFalse(sched.get(task.id).enabled)
        self.assertEqual(sched.get(task.id).run_count, 1)

    def test_run_now(self):
        fired = []
        done = __import__("threading").Event()

        def runner(task):
            fired.append(task.name)
            done.set()

        sched = Scheduler(runner=runner, poll=5.0)   # 轮询很慢，只靠手动触发
        sched.start()
        self.addCleanup(sched.stop)
        task = sched.create(name="手动", kind="daily", time_of_day="23:59")

        self.assertTrue(sched.run_now(task.id))
        self.assertTrue(done.wait(3.0))
        self.assertEqual(fired, ["手动"])
        self.assertEqual(sched.get(task.id).run_count, 0, "手动执行不该计入计划次数")

    def test_unknown_kind_is_safe(self):
        task = Task(name="t", kind="nonsense")
        self.assertIsNone(task.compute_next())


# ---------------------------------------------------------------------------
class TestMotion(unittest.TestCase):
    def test_simplify_keeps_endpoints(self):
        points = [(0, 0), (5, 0), (10, 0), (15, 0), (20, 0)]
        self.assertEqual(motion.simplify_indices(points, 1.0), [0, 4])
        # 误差为 0 时不简化
        self.assertEqual(motion.simplify_indices(points, 0), [0, 1, 2, 3, 4])

    def test_simplify_keeps_corner(self):
        points = [(0, 0), (10, 0), (20, 0), (20, 10), (20, 20)]
        kept = motion.simplify_indices(points, 1.0)
        self.assertIn(0, kept)
        self.assertIn(2, kept, "拐点应该保留")
        self.assertIn(4, kept)
        self.assertLess(len(kept), len(points))

    def test_filter_events_reduces_collinear_moves(self):
        events = [{"t": i * 0.02, "type": "mouse_move", "x": i * 10, "y": 50}
                  for i in range(60)]
        events.append({"t": 2.0, "type": "key_press", "key": "a"})
        filtered, stats = motion.filter_events(events, motion.get_profile("standard"))
        moves = [e for e in filtered if e["type"] == "mouse_move"]
        self.assertEqual(stats.raw_moves, 60)
        self.assertLessEqual(len(moves), 3, f"直线移动保留太多点: {len(moves)}")
        self.assertEqual(moves[0]["x"], 0)
        self.assertEqual(moves[-1]["x"], 590)
        self.assertEqual(filtered[-1]["type"], "key_press")

    def test_filter_preserves_other_events(self):
        filtered, stats = motion.filter_events(SAMPLE_EVENTS, motion.get_profile("compact"))
        self.assertEqual(stats.raw_moves, 1)
        self.assertEqual([e["type"] for e in filtered],
                         [e["type"] for e in SAMPLE_EVENTS])

    def test_profile_lookup(self):
        self.assertEqual(motion.get_profile("compact").key, "compact")
        self.assertEqual(motion.get_profile("精简").key, "compact")
        self.assertEqual(motion.get_profile("不认识").key, motion.DEFAULT_PROFILE)
        self.assertEqual(motion.get_profile(None).key, motion.DEFAULT_PROFILE)


# ---------------------------------------------------------------------------
class TestConfig(TempDirMixin, unittest.TestCase):
    def test_defaults_and_dotted_set(self):
        path = os.path.join(self.tmp, "config.json")
        cfg = config_module.Config(path)
        self.assertEqual(cfg.get("mouse_profile"), "standard")
        self.assertEqual(cfg.get("overlay.corner"), "top_right")
        self.assertEqual(cfg.get("not.exist", "x"), "x")

        cfg.set("overlay.text_color", "#ff0000")
        self.assertTrue(os.path.exists(path))

        again = config_module.Config(path)
        self.assertEqual(again.get("overlay.text_color"), "#ff0000")
        self.assertEqual(again.get("overlay.alpha"), 0.92, "缺失字段应补默认值")

    def test_broken_file_falls_back(self):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        cfg = config_module.Config(path)
        self.assertEqual(cfg.get("mouse_profile"), "standard")


# ---------------------------------------------------------------------------
class _FakeInjector:
    """前 N 次注入失败，之后成功的假注入器。"""

    name = "fake"

    def __init__(self, fail_count: int = 1):
        self.remaining = int(fail_count)
        self.calls = []
        self.failed = 0

    def _try(self):
        self.calls.append(1)
        if self.remaining > 0:
            self.remaining -= 1
            self.failed += 1
            return False
        return True

    def press_key(self, key):
        return self._try()

    def release_key(self, key):
        self._try()
        return True

    def move(self, x, y):
        return self._try()

    def button(self, button, press):
        return self._try()

    def scroll(self, dx, dy):
        return self._try()

    def take_error(self):
        return 5


class TestInputBlocked(unittest.TestCase):
    def test_stop_policy_aborts_with_reason(self):
        injector = _FakeInjector(fail_count=99)
        states = []
        player = Player(
            injector=injector, abort_keys=(),
            on_state=lambda state, info: states.append((state, info)),
        )
        result = player.play([
            {"t": 0.0, "type": "key_press", "key": "a"},
            {"t": 0.01, "type": "mouse_move", "x": 10, "y": 10},
        ])
        self.assertTrue(result["blocked"])
        self.assertTrue(result["aborted"])
        self.assertIn("blocked", [s for s, _ in states])
        self.assertTrue(result["reason"])
        self.assertEqual(result["failed"], 1)

    def test_skip_policy_continues(self):
        injector = _FakeInjector(fail_count=1)
        player = Player(
            injector=injector, abort_keys=(),
            on_input_blocked="skip",
        )
        result = player.play([
            {"t": 0.0, "type": "key_press", "key": "a"},
            {"t": 0.01, "type": "mouse_move", "x": 10, "y": 10},
        ])
        self.assertFalse(result["blocked"])
        self.assertFalse(result["aborted"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["played"], 1)


# ---------------------------------------------------------------------------
class TestRecorderOptimisation(unittest.TestCase):
    def _make(self, **kwargs):
        self.stopped = []
        rec = Recorder(on_stop=lambda events: self.stopped.append(events), **kwargs)
        self.addCleanup(rec.stop)
        return rec

    def test_profile_applied_on_stop(self):
        rec = self._make(profile="compact")
        # 测试里连续调用 _on_move 没有真实时间间隔，先去掉按时间采样的限制，
        # 单独验证「按距离过滤 + 路径简化」。
        rec.profile = motion.MouseProfile(
            key="test", label="测试", min_distance=4, min_interval=0.0,
            simplify=3.0, description="",
        )
        self.assertTrue(rec.start_recording(mouse_grace=0.0))
        for i in range(40):
            rec._on_move(i * 10, 100)
        rec._on_press(keyboard.KeyCode.from_char("a"))
        rec._on_release(keyboard.KeyCode.from_char("a"))
        events = rec.stop_recording()
        moves = [e for e in events if e["type"] == "mouse_move"]
        self.assertLess(len(moves), 10, f"精简后仍然有 {len(moves)} 个移动点")
        self.assertEqual(moves[0]["x"], 0)
        self.assertEqual(moves[-1]["x"], 390)
        self.assertIsNotNone(rec.last_filter_stats)
        self.assertGreater(rec.last_filter_stats.removed, 0)

    def test_ignore_rect(self):
        rec = self._make()
        rec.set_ignore_rect((0, 0, 200, 200))
        rec.start_recording(mouse_grace=0.0)
        button = type("B", (), {"name": "left"})()
        rec._on_click(50, 50, button, True)      # 悬浮窗内，丢弃
        rec._on_move(60, 60)                     # 悬浮窗内，丢弃
        rec._on_click(400, 300, button, True)    # 正常记录
        rec._on_move(420, 320)
        events = rec.stop_recording()
        self.assertEqual([e["type"] for e in events], ["mouse_click", "mouse_move"])
        self.assertEqual(events[0]["x"], 400)

    def test_cancel_recording_discards(self):
        rec = self._make()
        rec.start_recording(mouse_grace=0.0)
        rec._on_press(keyboard.KeyCode.from_char("x"))
        rec.cancel_recording()
        self.assertFalse(rec.recording)
        self.assertEqual(rec.events, [])
        self.assertEqual(self.stopped, [], "取消不应触发保存回调")

    def test_live_snapshot(self):
        rec = self._make()
        rec.start_recording(mouse_grace=0.0)
        rec._on_move(321, 123)
        rec._on_press(keyboard.KeyCode.from_char("q"))
        snap = rec.snapshot()
        self.assertTrue(snap["recording"])
        self.assertEqual(snap["pos"], (321, 123))
        self.assertIn("q", snap["keys"])
        rec.cancel_recording()


if __name__ == "__main__":
    unittest.main(verbosity=2)
