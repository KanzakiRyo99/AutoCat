"""GUI 端到端自测：真的建窗口、点按钮、走一遍录制→保存→回放→定时。

截图只能证明「长得对」，这个文件证明「接得对」：
- 控件回调是否连上、状态是否正确切换
- 录制保存后列表是否自动刷新
- 回放是否真的经过 Player（用 dry_run 的 Player 替换，不碰真实键鼠）
- 定时任务是否能添加、持久化、手动执行

运行：
    python tests/test_gui.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import tkinter as tk
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pynput import keyboard  # noqa: E402

from autocat import gui as gui_module  # noqa: E402
from autocat import storage  # noqa: E402
from autocat.gui import AutoCatGUI  # noqa: E402
from autocat.player import Player  # noqa: E402

DEMO_EVENTS = [
    {"t": 0.0, "type": "key_press", "key": "a"},
    {"t": 0.05, "type": "key_release", "key": "a"},
    {"t": 0.1, "type": "mouse_move", "x": 10, "y": 10},
]


class _FakeButton:
    name = "left"


class TestGuiEndToEnd(unittest.TestCase):
    """整个类共用一个窗口实例，避免反复建 Tk 拖慢测试。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = cls.tmp.name
        cls.rec_dir = os.path.join(cls.base, "recordings")
        os.makedirs(cls.rec_dir, exist_ok=True)
        for name in ("demo_a.json", "demo_b.xml"):
            storage.save_recording(os.path.join(cls.rec_dir, name), DEMO_EVENTS)

        # 别让模态框把测试卡死
        cls._mb_patcher = mock.patch.object(gui_module, "messagebox")
        cls.mb = cls._mb_patcher.start()
        cls.mb.askokcancel.return_value = True
        cls.mb.askyesno.return_value = True
        cls.mb.askstring.return_value = "renamed"

        # 回放统一替换成 dry_run 的 Player：走真实线程和时序，但不真发事件
        cls._player_patcher = mock.patch.object(
            gui_module, "Player",
            side_effect=lambda **kwargs: Player(dry_run=True, speed=200.0,
                                                **{k: v for k, v in kwargs.items()
                                                   if k in ("loops", "on_state")}),
        )
        cls._player_patcher.start()

        cls.root = tk.Tk()
        cls.root.withdraw()          # 测试时不弹到屏幕上
        cls.gui = AutoCatGUI(cls.root, cls.base)
        cls.pump(0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.gui.shutdown()
            cls.root.destroy()
        finally:
            cls._player_patcher.stop()
            cls._mb_patcher.stop()
            cls.tmp.cleanup()

    # ------------------------------------------------------------------
    @classmethod
    def pump(cls, seconds: float = 0.2) -> None:
        """跑一会儿事件循环，让 after 回调和后台线程把结果送回主线程。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            cls.root.update()
            time.sleep(0.01)

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.root.update()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------
    def test_01_recordings_listed(self):
        rows = self.gui.tree.get_children()
        self.assertEqual(len(rows), 2, "初始应该列出两个演示文件")
        names = {self.gui.tree.item(r, "text") for r in rows}
        self.assertEqual(names, {"demo_a.json", "demo_b.xml"})

    def test_02_record_save_and_refresh(self):
        before = len(self.gui.tree.get_children())

        self.gui.start_recording()
        self.assertTrue(self.gui.recorder.recording, "点开始后应该处于录制中")
        self.assertEqual(str(self.gui.btn_stop["state"]), "normal")
        self.assertEqual(str(self.gui.btn_record["state"]), "disabled")

        # 点「开始录制」这一下本身不该被录进去：鼠标事件有 0.25s 宽限期，
        # 这里先验证宽限期内的事件被丢弃，再等过宽限期录真正的事件。
        self.gui.recorder._on_click(1, 2, _FakeButton(), False)
        self.assertEqual(self.gui.recorder.events, [], "宽限期内不该记录鼠标事件")
        time.sleep(0.3)

        # 直接喂事件，等价于用户敲了一下 'a'
        self.gui.recorder._on_press(keyboard.KeyCode.from_char("a"))
        self.gui.recorder._on_release(keyboard.KeyCode.from_char("a"))
        self.gui.recorder._on_click(5, 6, _FakeButton(), True)

        self.gui.stop_recording()
        self.assertFalse(self.gui.recorder.recording)

        self.assertTrue(
            self.wait_for(lambda: len(self.gui.tree.get_children()) == before + 1),
            "录制保存后列表没有自动刷新",
        )
        new_path = self.gui._last_path
        self.assertTrue(os.path.exists(new_path))
        payload = storage.load_recording(new_path)
        self.assertEqual(
            [e["type"] for e in payload["events"]],
            ["key_press", "key_release", "mouse_click"],
        )
        self.assertEqual(payload["meta"]["source"], "gui")
        # 新文件应被自动选中
        self.assertEqual(self.gui.tree.selection(), (new_path,))

    def test_03_xml_format_selected(self):
        self.gui.var_format.set("xml")
        self.gui.start_recording()
        self.gui.recorder._on_press(keyboard.KeyCode.from_char("z"))
        self.gui.recorder._on_release(keyboard.KeyCode.from_char("z"))
        self.gui.stop_recording()
        self.assertTrue(self.wait_for(lambda: self.gui._last_path.endswith(".xml")))
        self.assertTrue(os.path.exists(self.gui._last_path))
        # 收尾：切回 json，别影响后面的用例
        self.gui.var_format.set("json")

    def test_04_playback_flow(self):
        path = os.path.join(self.rec_dir, "demo_a.json")
        self.gui.tree.selection_set(path)
        self.gui.play_selected()

        self.assertTrue(self.gui._play_lock.locked(), "回放开始时应持有播放锁")
        self.assertFalse(self.gui.recorder.hotkeys_enabled,
                         "回放期间应该关掉 F9 热键，否则回放的事件会被录进去")

        self.assertTrue(self.wait_for(lambda: not self.gui._play_lock.locked()),
                        "回放没有正常结束")
        self.pump(0.3)
        self.assertTrue(self.gui.recorder.hotkeys_enabled, "回放结束后热键要恢复")
        self.assertEqual(str(self.gui.btn_play["state"]), "normal")

    def test_05_playback_rejects_missing_selection(self):
        self.gui.tree.selection_remove(*self.gui.tree.selection())
        self.gui.play_selected()
        self.mb.showinfo.assert_called()
        self.assertFalse(self.gui._play_lock.locked())

    def test_06_add_schedule_task(self):
        self.gui.var_task_name.set("测试任务")
        self.gui.var_task_file.set("demo_b.xml")
        self.gui.var_kind.set("固定间隔")
        self.gui.var_seconds.set(120)
        self.gui.var_task_speed.set("1.5")
        self.gui.var_task_loops.set(2)

        self.gui.add_task()

        tasks = self.gui.scheduler.list_tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.name, "测试任务")
        self.assertEqual(task.kind, "interval")
        self.assertEqual(task.seconds, 120)
        self.assertTrue(task.file.endswith("demo_b.xml"))
        self.assertIsNotNone(task.next_run, "间隔任务应该有下次执行时间")

        # 列表和持久化文件都要跟上
        self.assertEqual(len(self.gui.task_tree.get_children()), 1)
        self.assertTrue(os.path.exists(os.path.join(self.base, "schedules.json")))

    def test_07_task_select_fills_form(self):
        task_id = self.gui.task_tree.get_children()[0]
        self.gui.task_tree.selection_set(task_id)
        self.gui._on_task_select()
        self.assertEqual(self.gui.var_task_name.get(), "测试任务")
        self.assertEqual(self.gui.var_task_file.get(), "demo_b.xml")
        self.assertEqual(self.gui.var_kind.get(), "固定间隔")
        self.assertEqual(self.gui.var_seconds.get(), 120)

    def test_08_all_kinds_show_correct_widget(self):
        """切换触发方式时，参数输入框应该只显示对应的那一个。"""
        # 先清空任务选中：选中事件会在 update() 时回调 _on_task_select，
        # 那会把 var_kind 改回被选中任务的类型，干扰本用例。
        self.gui.task_tree.selection_remove(*self.gui.task_tree.selection())
        self.root.update()

        cases = {
            "指定时刻一次": (self.gui.w_at, (self.gui.w_seconds, self.gui.w_tod)),
            "延时一次": (self.gui.w_seconds, (self.gui.w_at, self.gui.w_tod)),
            "固定间隔": (self.gui.w_seconds, (self.gui.w_at, self.gui.w_tod)),
            "每天定时": (self.gui.w_tod, (self.gui.w_at, self.gui.w_seconds)),
        }
        for label, (shown, hidden) in cases.items():
            self.gui.var_kind.set(label)
            self.gui._on_kind_change()
            # 用 grid_info() 判断：grid_remove() 之后的控件 grid_info() 是空字典，
            # 而且它不依赖窗口是否已映射（测试里窗口是 withdrawal 的）。
            self.assertTrue(shown.grid_info(), f"{label}: 应显示 {shown}")
            for widget in hidden:
                self.assertFalse(widget.grid_info(), f"{label}: 不应显示 {widget}")

        self.gui.var_kind.set("固定间隔")
        self.gui._on_kind_change()

    def test_09_toggle_and_delete_task(self):
        task_id = self.gui.task_tree.get_children()[0]
        self.gui.task_tree.selection_set(task_id)

        self.gui.toggle_task()
        self.assertFalse(self.gui.scheduler.get(task_id).enabled)
        # 刷新后选中项应该还在（回归测试：以前刷新会把选中弄丢）
        self.assertIn(task_id, self.gui.task_tree.selection(),
                      "刷新任务列表后不该丢失选中项")
        self.gui.task_tree.selection_set(task_id)
        self.gui.toggle_task()
        self.assertTrue(self.gui.scheduler.get(task_id).enabled)

        self.gui.task_tree.selection_set(task_id)
        self.gui.delete_task()
        self.assertEqual(self.gui.scheduler.list_tasks(), [])

    def test_10_scheduled_task_actually_runs(self):
        """端到端：调度器到点 -> 回调 -> 走 Player 回放 -> 状态复位。"""
        self.gui.var_task_name.set("马上跑")
        self.gui.var_task_file.set("demo_a.json")
        self.gui.var_kind.set("延时一次")
        self.gui.var_seconds.set(1)
        self.gui.add_task()

        task = self.gui.scheduler.list_tasks()[0]
        task.anchor = datetime.now().isoformat()  # 确保 1 秒后触发
        task.refresh_next()

        self.assertTrue(
            self.wait_for(lambda: task.run_count >= 1, timeout=6.0),
            "定时任务没有触发",
        )
        self.assertTrue(
            self.wait_for(lambda: not self.gui._play_lock.locked(), timeout=6.0),
            "定时任务触发的回放没有结束",
        )
        self.assertFalse(task.enabled, "延时任务跑完应该自动停用")

    def test_11_log_panel_receives_messages(self):
        self.pump(0.3)
        text = self.gui.log_text.get("1.0", "end")
        self.assertIn("AutoCat v", text, "启动日志应该显示在日志面板里")

    def test_12_state_badge(self):
        self.gui._update_state_badge()
        self.assertEqual(self.gui.lbl_badge["text"], "空闲")
        self.gui._update_state_badge(playing=True)
        self.assertEqual(self.gui.lbl_badge["text"], "回放中")
        self.gui._update_state_badge(recording=True)
        self.assertIn("录制中", self.gui.lbl_badge["text"])
        self.gui._update_state_badge()

    def test_13_editor_tab_loads_selection(self):
        path = os.path.join(self.rec_dir, "demo_a.json")
        self.gui.tree.selection_set(path)
        self.gui.open_editor()
        self.assertEqual(self.gui.nb.select(), str(self.gui.tab_editor))
        self.assertEqual(self.gui.editor.path, path)
        self.assertGreater(len(self.gui.editor.events), 0)

    def test_14_mouse_profile_switch(self):
        self.gui.var_profile.set("精简")
        self.gui._on_profile_change()
        self.assertEqual(self.gui.recorder.profile.key, "compact")
        self.assertEqual(self.gui.recorder.move_threshold, 4)
        self.assertEqual(self.gui.config.get("mouse_profile"), "compact")

        self.gui.var_profile.set("标准")
        self.gui._on_profile_change()
        self.assertEqual(self.gui.recorder.profile.key, "standard")

    def test_15_overlay_exists_and_settings_open(self):
        self.assertIsNotNone(self.gui.overlay)
        self.assertFalse(self.gui.overlay.visible)

        self.gui.open_settings()
        root = self.gui.root
        root.update()
        dialogs = [
            w for w in root.winfo_children()
            if isinstance(w, tk.Toplevel) and w.title() == "设置"
        ]
        self.assertTrue(dialogs, "设置窗口没有打开")
        dialogs[0].destroy()
        root.update()


if __name__ == "__main__":
    unittest.main(verbosity=2)
