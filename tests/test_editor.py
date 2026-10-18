"""录制内容编辑器自测。

真正建一个 Tk 窗口（但不显示），验证分组、折叠、编辑、插入、删除、精简、
保存回盘。弹窗一律 mock 掉，避免测试卡在模态对话框上。
"""

from __future__ import annotations

import os
import sys
import tempfile
import tkinter as tk
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocat import editor as editor_module  # noqa: E402
from autocat import storage  # noqa: E402
from autocat.editor import RecordingEditor  # noqa: E402

MOVES_A = [
    {"t": round(i * 0.05, 3), "type": "mouse_move", "x": 100 + i * 5, "y": 200}
    for i in range(20)
]
MOVES_B = [
    {"t": round(2.0 + i * 0.05, 3), "type": "mouse_move", "x": 400, "y": 300 + i * 4}
    for i in range(12)
]
EVENTS = (
    MOVES_A
    + [
        {"t": 1.2, "type": "key_press", "key": "a"},
        {"t": 1.25, "type": "key_release", "key": "a"},
        {"t": 1.5, "type": "mouse_click", "x": 500, "y": 400, "button": "right", "pressed": True},
        {"t": 1.55, "type": "mouse_click", "x": 500, "y": 400, "button": "right", "pressed": False},
        {"t": 1.7, "type": "mouse_scroll", "x": 500, "y": 400, "dx": 0, "dy": -3},
    ]
    + MOVES_B
)


class TestRecordingEditor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "edit.json")
        storage.save_recording(cls.path, EVENTS, meta={"source": "test", "note": "保留我"})

        cls._mb = mock.patch.object(editor_module, "messagebox")
        cls.mb = cls._mb.start()
        cls.mb.askyesno.return_value = True

        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.editor = RecordingEditor(cls.root)
        cls.editor.pack(fill="both", expand=True)
        cls.editor.load_path(cls.path)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.editor.destroy()
            cls.root.destroy()
        finally:
            cls._mb.stop()
            cls.tmp.cleanup()

    def setUp(self):
        # 每个用例重新载入，互不污染
        self.editor.load_path(self.path)
        self.root.update()

    # ------------------------------------------------------------------
    def test_grouping_and_default_collapsed(self):
        children = self.editor.tree.get_children()
        self.assertEqual(len(children), 2 + 5, "两段鼠标移动 + 5 条独立事件")
        groups = [iid for iid in children if iid in self.editor._groups]
        self.assertEqual(len(groups), 2)
        for gid in groups:
            self.assertFalse(self.editor.tree.item(gid, "open"), "鼠标移动段默认应折叠")
            self.assertEqual(len(self.editor.tree.get_children(gid)), 20 if gid == groups[0] else 12)

        first_group = groups[0]
        self.assertIn("20 个点", self.editor.tree.item(first_group, "text"))

    def test_expand_and_edit_point(self):
        gid = next(iter(self.editor._groups))
        self.editor.tree.item(gid, open=True)
        self.editor.expand_all(True)
        self.assertTrue(self.editor.tree.item(gid, "open"))

        index = 3
        self.editor.update_event(index, {
            "t": self.editor.events[index]["t"],
            "type": "mouse_move", "x": 999, "y": 888,
        })
        self.assertEqual(self.editor.events[index]["x"], 999)
        self.assertTrue(self.editor.dirty)
        self.assertTrue(self.editor.tree.exists(f"e:{index}"))

    def test_insert_before_selected(self):
        before = len(self.editor.events)
        self.editor.tree.selection_set("e:2")
        self.editor.insert_event(before=True, event={"t": 0.11, "type": "key_press", "key": "z"})
        self.assertEqual(len(self.editor.events), before + 1)
        self.assertEqual(self.editor.events[2]["key"], "z")
        self.assertIn("e:2", self.editor.tree.selection())

    def test_insert_after_group(self):
        before = len(self.editor.events)
        gid = next(iter(self.editor._groups))
        self.editor.tree.selection_set(gid)
        self.editor.insert_event(before=False, event={"t": 1.1, "type": "key_press", "key": "q"})
        self.assertEqual(len(self.editor.events), before + 1)
        # 插在第一段鼠标移动之后（第 20 条之后）
        self.assertEqual(self.editor.events[20]["key"], "q")

    def test_delete_whole_mouse_group(self):
        gid = next(iter(self.editor._groups))
        self.editor.tree.selection_set(gid)
        self.editor.delete_selected()
        self.assertEqual(len(self.editor.events), len(EVENTS) - 20)
        self.assertEqual(
            len([e for e in self.editor.events if e["type"] == "mouse_move"]), 12
        )

    def test_simplify_moves(self):
        self.editor.cmb_profile.set("精简")
        self.editor.simplify_moves()
        moves = [e for e in self.editor.events if e["type"] == "mouse_move"]
        self.assertLess(len(moves), len(MOVES_A) + len(MOVES_B))
        self.assertGreater(len(moves), 0)
        self.assertTrue(self.editor.dirty)

    def test_undo(self):
        before = [dict(e) for e in self.editor.events]
        gid = next(iter(self.editor._groups))
        self.editor.tree.selection_set(gid)
        self.editor.delete_selected()
        self.editor.undo()
        self.assertEqual(self.editor.events, before)

    def test_save_roundtrip_preserves_meta_and_edit(self):
        index = 4
        self.editor.update_event(index, {
            "t": self.editor.events[index]["t"],
            "type": "mouse_move", "x": 12345, "y": 6789,
        })
        self.assertTrue(self.editor.save())

        payload = storage.load_recording(self.path)
        self.assertEqual(payload["meta"]["note"], "保留我")
        self.assertEqual(payload["events"][index]["x"], 12345)
        self.assertFalse(self.editor.dirty)
        self.assertGreater(len(payload["events"]), 0)

        # 收尾：把改动写回去，避免影响后面的用例
        storage.save_recording(self.path, EVENTS, meta={"source": "test", "note": "保留我"})

    def test_save_sorts_by_time(self):
        self.editor.events[0] = dict(self.editor.events[0], t=99.0)
        self.editor.save()
        payload = storage.load_recording(self.path)
        times = [float(e["t"]) for e in payload["events"]]
        self.assertEqual(times, sorted(times))
        storage.save_recording(self.path, EVENTS, meta={"source": "test", "note": "保留我"})

    def test_edit_and_save_xml(self):
        xml_path = os.path.join(self.tmp.name, "edit.xml")
        storage.save_recording(xml_path, EVENTS, meta={"source": "test"})
        self.assertTrue(self.editor.load_path(xml_path))

        before = len(self.editor.events)
        self.editor.insert_event(before=False, event={"t": 3.5, "type": "key_press", "key": "x"})
        self.assertTrue(self.editor.save())

        with open(xml_path, encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("<?xml"))
        payload = storage.load_recording(xml_path)
        self.assertEqual(len(payload["events"]), before + 1)
        self.assertEqual(payload["events"][-1]["key"], "x")
        self.assertEqual(payload["meta"]["source"], "test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
